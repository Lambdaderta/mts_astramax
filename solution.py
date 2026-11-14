"""Official CSV platform adapter, independent of FastAPI and persistent sessions.

The platform supplies DATA_DIR/questions.csv, DATA_DIR/knowledge-base.zip and
the model proxy credentials. The default output is ./submission.csv. Use
--offline explicitly for a local extractive baseline, never as a scored submit.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from app.ml.adapter import LLMConfig, build_core
from app.ml.corpus import SUPPORTED_SUFFIXES, ingest
from app.ml.environment import env_value
from app.ml.protocol import (
    ConsultationRequest, ConsultationUnavailable, ContextMessage, InvalidEvidence,
)
from app.privacy import extract_contacts, manager_requested, redact


MAX_ARCHIVE_FILES = 10000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_QUESTIONS_BYTES = 16 * 1024 * 1024
MAX_ANSWER_CHARACTERS = 10000
FAILURE_ANSWER = (
    "Сейчас не удалось получить подтвержденный ответ по базе знаний. "
    "Не буду придумывать характеристики или условия. Предлагаю передать вопрос специалисту."
)
INVALID_QUESTION_ANSWER = (
    "Не удалось определить последний вопрос пользователя в переданном диалоге. "
    "Уточните вопрос, сохранив важные условия задачи."
)
CONTACT_REQUEST_ANSWER = (
    "Для обращения к специалисту укажите телефон для связи. "
    "Если удобно, добавьте ваше имя и название компании."
)
DIALOGUE_LABEL = re.compile(
    r"^[ \t]*(Пользователь|Клиент|User|Human|Ассистент|Assistant|Бот)[ \t]*:[ \t]*",
    re.IGNORECASE | re.MULTILINE,
)
USER_LABELS = {"пользователь", "клиент", "user", "human"}


class SubmissionError(ValueError):
    """A sanitized, actionable input/configuration error safe for stderr."""


@dataclass(frozen=True)
class Question:
    id: str
    text: str


def find_input_file(data_dir: Path, *names: str) -> Path:
    """Match the organizer's DATA_DIR, script-relative and cwd lookup order.

    Within each location the documented filename precedes compatibility names.
    """
    for root in (data_dir, Path(__file__).resolve().parent / data_dir, Path.cwd()):
        for name in names:
            path = root / name
            if path.is_file():
                return path
    raise SubmissionError(
        f"Could not find {' or '.join(names)} in DATA_DIR, script-relative DATA_DIR or the working directory"
    )


def read_questions(path: Path) -> list[Question]:
    if not path.is_file():
        raise SubmissionError("questions.csv is missing from DATA_DIR")
    if path.stat().st_size > MAX_QUESTIONS_BYTES:
        raise SubmissionError("questions.csv exceeds the local 16 MiB input limit")
    csv.field_size_limit(1_000_000)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames or []
        if len(set(columns)) != len(columns) or not {"id", "question"}.issubset(columns):
            raise SubmissionError("questions.csv must contain distinct id and question columns")
        seen: set[str] = set()
        questions: list[Question] = []
        for row in reader:
            if None in row or not isinstance(row.get("id"), str) or not isinstance(row.get("question"), str):
                raise SubmissionError("questions.csv contains a malformed row")
            identifier, question = row["id"].strip(), row["question"].strip()
            if not identifier or not question:
                raise SubmissionError("Every input row requires a nonempty id and question")
            if identifier in seen:
                raise SubmissionError("questions.csv contains duplicate IDs")
            seen.add(identifier)
            questions.append(Question(identifier, question))
    if not questions:
        raise SubmissionError("questions.csv has no questions")
    return questions


def extract_knowledge(archive_path: Path, destination: Path) -> int:
    """Extract supported documents into a new private temporary directory.

    Do not trust ZIP paths, link modes, declared sizes or compression ratios.
    The corpus is provided at execution time and is never packed in the submit.
    """
    if not archive_path.is_file():
        raise SubmissionError("knowledge-base.zip is missing from DATA_DIR")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    count = 0
    with ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise SubmissionError("Knowledge archive has too many entries")
        if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
            raise SubmissionError("Knowledge archive exceeds the 256 MiB extraction limit")
        targets: set[str] = set()
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            if (
                not name or "\\" in name or "\x00" in entry.orig_filename
                or path.is_absolute() or ".." in path.parts
                or any(":" in part for part in path.parts)
            ):
                raise SubmissionError("Knowledge archive contains an unsafe path")
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise SubmissionError("Knowledge archive contains a link or special file")
            if entry.flag_bits & 1:
                raise SubmissionError("Encrypted knowledge archives are not supported")
            if entry.is_dir() or "__MACOSX" in path.parts or path.name.startswith("."):
                continue
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            normalized = path.as_posix().casefold()
            if normalized in targets:
                raise SubmissionError("Knowledge archive contains duplicate document paths")
            targets.add(normalized)
            if entry.file_size > MAX_MEMBER_BYTES:
                raise SubmissionError("A knowledge document exceeds the 32 MiB limit")
            target = destination.joinpath(*path.parts)
            if root not in target.resolve().parents:
                raise SubmissionError("Knowledge archive path escapes its destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with archive.open(entry) as source, target.open("xb") as output:
                while chunk := source.read(65536):
                    size += len(chunk)
                    if size > MAX_MEMBER_BYTES or size > entry.file_size:
                        raise SubmissionError("Knowledge document exceeds its declared size")
                    output.write(chunk)
            count += 1
    if not count:
        raise SubmissionError("Knowledge archive has no supported text documents")
    return count


def consultation_request(question: Question, corpus_version: str, timeout_ms: int) -> ConsultationRequest:
    """Parse the role labels used by the organizer's published dialogue examples."""
    markers = list(DIALOGUE_LABEL.finditer(question.text))
    messages: list[ContextMessage] = []
    if not markers:
        messages.append(ContextMessage(id=str(uuid4()), role="user", text=question.text))
    else:
        prefix = question.text[:markers[0].start()].strip()
        if prefix:
            messages.append(ContextMessage(id=str(uuid4()), role="user", text=prefix))
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(question.text)
            content = question.text[marker.end():end].strip()
            if content:
                role = "user" if marker.group(1).lower() in USER_LABELS else "assistant"
                messages.append(ContextMessage(id=str(uuid4()), role=role, text=content))
    user_positions = [index for index, message in enumerate(messages) if message.role == "user"]
    if not user_positions:
        raise SubmissionError("A dialogue case must contain a user message")
    messages = messages[:user_positions[-1] + 1]
    known_values = [
        value
        for message in messages if message.role == "user"
        for value in extract_contacts(message.text).values() if value
    ]
    messages = [message.model_copy(update={"text": redact(message.text, known_values=known_values)})
                for message in messages]
    latest = messages[-1]
    return ConsultationRequest(
        request_id=str(uuid4()), session_id=str(uuid4()), user_message_id=latest.id,
        latest_user_text=latest.text, messages=messages, current_phase="new",
        corpus_version=corpus_version, timeout_budget_ms=timeout_ms,
    )


async def answer_questions(
    questions: list[Question], core, *, workers: int = 3,
    case_timeout_seconds: float = 120, total_timeout_seconds: float = 1080,
    retrieval_counts: dict[str, int] | None = None,
) -> tuple[list[tuple[str, str]], int]:
    deadline = time.monotonic() + total_timeout_seconds
    answers = [(question.id, FAILURE_ANSWER) for question in questions]
    failures = len(questions)
    pending = iter(enumerate(questions))

    async def worker() -> None:
        nonlocal failures
        for index, question in pending:
            remaining = min(case_timeout_seconds, deadline - time.monotonic())
            if remaining <= 0:
                return
            try:
                try:
                    request = consultation_request(question, core.corpus_version, max(1, int(remaining * 1000)))
                except ValueError:
                    # Do not guess a user question from a malformed role transcript.
                    answers[index] = question.id, INVALID_QUESTION_ANSWER
                    continue
                if manager_requested(request.latest_user_text):
                    answers[index] = question.id, CONTACT_REQUEST_ANSWER
                    failures -= 1
                    continue
                async with asyncio.timeout(min(remaining, deadline - time.monotonic())):
                    result = await core.consult(request)
                if retrieval_counts is not None:
                    retrieval_mode = result.model_metadata.get("retrieval_mode", "unknown")
                    retrieval_counts[retrieval_mode] = retrieval_counts.get(retrieval_mode, 0) + 1
                core.validate_result(result)
                text = result.answer_text.strip()
                if not text or len(text) > MAX_ANSWER_CHARACTERS:
                    raise InvalidEvidence("Invalid output length")
            except (ConsultationUnavailable, InvalidEvidence, ValueError, TimeoutError):
                continue
            except Exception as error:
                # Isolate unexpected provider/core errors to their case. Error
                # messages may contain credentials or source text, so omit them.
                print(
                    f"Submission case failed: phase=case_failed; case_index={index + 1}; "
                    f"exception_type={type(error).__name__}.", file=sys.stderr, flush=True,
                )
                continue
            answers[index] = question.id, text
            failures -= 1

    await asyncio.gather(*(worker() for _ in range(min(workers, len(questions)))))
    return answers, failures


def write_submission(path: Path, answers: list[tuple[str, str]]) -> None:
    identifiers = [identifier for identifier, _ in answers]
    if len(set(identifiers)) != len(identifiers):
        raise SubmissionError("Refusing to write duplicate output IDs")
    if any(not answer.strip() or len(answer) > MAX_ANSWER_CHARACTERS for _, answer in answers):
        raise SubmissionError("An answer is empty or exceeds 10000 characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=".submission-", suffix=".csv.tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(["id", "answer"])
            writer.writerows(answers)
            stream.flush()
            # The platform may read this result from a separate grading user.
            # Private corpus/index/cache files retain their existing permissions.
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def environment_number(name: str, default: float, lower: float, upper: float, *, integer=False):
    value = env_value(name)
    if not value:
        return int(default) if integer else default
    try:
        parsed = int(value) if integer else float(value)
    except ValueError as error:
        raise SubmissionError(f"{name} must be numeric") from error
    if not lower <= parsed <= upper:
        raise SubmissionError(f"{name} is outside the supported range")
    return parsed


def run_submission(data_dir: Path, output: Path, *, offline: bool = False) -> dict[str, int | str]:
    started = time.monotonic()
    # Missing credentials are a configuration failure, never an implicit offline run.
    config = None if offline else LLMConfig.from_env(platform=True)
    questions = read_questions(find_input_file(data_dir, "questions.csv", "train.csv"))
    knowledge_path = find_input_file(data_dir, "knowledge-base.zip", "knowledge_base.zip", "kb.zip")
    workers = environment_number("MAX_WORKERS", 3, 1, 8, integer=True)
    case_timeout = environment_number("CONSULTATION_TIMEOUT_SECONDS", 120, 1, 120)
    total_timeout = environment_number("SUBMISSION_TIMEOUT_SECONDS", 1080, 1, 1140)
    with tempfile.TemporaryDirectory(prefix="energytool-submit-") as temporary:
        working = Path(temporary)
        document_count = extract_knowledge(knowledge_path, working / "corpus")
        corpus = ingest(working / "corpus")
        corpus.save(working / "index")
        core = build_core(working / "index", llm_config=config, platform=not offline)
        if not core.ready:
            raise SubmissionError("The supplied knowledge index is not ready")
        print(
            f"Submission inputs ready: questions={len(questions)}; documents={document_count}.",
            file=sys.stderr, flush=True,
        )
        retrieval_counts: dict[str, int] = {}
        async def execute():
            remaining = total_timeout - (time.monotonic() - started)
            await core.prepare_retrieval(timeout_budget_ms=max(1, int(min(120, remaining * 0.25) * 1000)))
            print(
                f"Submission retrieval ready: configured={core.retrieval_config.mode}; "
                f"fallback={core._prepare_failure or 'none'}.",
                file=sys.stderr, flush=True,
            )
            return await answer_questions(
                questions, core, workers=workers,
                case_timeout_seconds=case_timeout,
                total_timeout_seconds=total_timeout - (time.monotonic() - started),
                retrieval_counts=retrieval_counts,
            )
        answers, failures = asyncio.run(execute())
        print(
            f"Submission generation complete: answers={len(answers)}; fallbacks={failures}.",
            file=sys.stderr, flush=True,
        )
        if [identifier for identifier, _ in answers] != [question.id for question in questions]:
            raise SubmissionError("Output IDs do not match the input")
        write_submission(output, answers)
    completed_modes = {mode for mode, count in retrieval_counts.items() if count and mode != "not_run"}
    actual_retrieval = next(iter(completed_modes)) if len(completed_modes) == 1 else "mixed" if completed_modes else "not_run"
    if core._prepare_failure and not completed_modes:
        actual_retrieval = "lexical_fallback"
    return {
        "questions": len(questions), "documents": document_count,
        "fallbacks": failures, "mode": "offline" if offline else "platform-model",
        "retrieval": actual_retrieval,
        "configured_retrieval": core.retrieval_config.mode,
        "retrieval_fallback": core._prepare_failure or "none",
        "retrieval_counts": ",".join(f"{mode}:{count}" for mode, count in sorted(retrieval_counts.items())) or "none",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(env_value("DATA_DIR", default="data")))
    parser.add_argument("--output", type=Path, default=Path("submission.csv"))
    parser.add_argument("--offline", action="store_true", help="Explicit local-only extractive baseline")
    args = parser.parse_args(argv)
    runtime = ".".join(str(part) for part in sys.version_info[:3])
    print(f"Submission started: python={runtime}; validating configuration and inputs.", file=sys.stderr, flush=True)
    try:
        result = run_submission(args.data_dir, args.output, offline=args.offline)
    except SubmissionError as error:
        print(f"Submission input error: {error}", file=sys.stderr, flush=True)
        return 2
    except (OSError, ValueError, BadZipFile, csv.Error):
        # Provider credentials, document text and question content never enter logs.
        print("Submission failed: check input files, model environment and supported dependencies.", file=sys.stderr, flush=True)
        return 2
    print(
        f"Wrote submission.csv: {result['questions']} answers; "
        f"{result['documents']} documents; mode={result['mode']}; "
        f"fallbacks={result['fallbacks']}; retrieval={result['retrieval']}; "
        f"configured_retrieval={result['configured_retrieval']}; "
        f"startup_retrieval_fallback={result['retrieval_fallback']}; "
        f"retrieval_counts={result['retrieval_counts']}.", file=sys.stderr, flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
