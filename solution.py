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
from app.ml.protocol import (
    ConsultationRequest, ConsultationUnavailable, ContextMessage, InvalidEvidence,
)


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
    latest = messages[-1]
    return ConsultationRequest(
        request_id=str(uuid4()), session_id=str(uuid4()), user_message_id=latest.id,
        latest_user_text=latest.text, messages=messages, current_phase="new",
        corpus_version=corpus_version, timeout_budget_ms=timeout_ms,
    )


async def answer_questions(
    questions: list[Question], core, *, workers: int = 3,
    case_timeout_seconds: float = 60, total_timeout_seconds: float = 1080,
) -> tuple[list[tuple[str, str]], int]:
    semaphore = asyncio.Semaphore(workers)
    deadline = time.monotonic() + total_timeout_seconds
    failures = 0

    async def answer(question: Question) -> tuple[str, str]:
        nonlocal failures
        async with semaphore:
            remaining = min(case_timeout_seconds, deadline - time.monotonic())
            if remaining <= 0:
                failures += 1
                return question.id, FAILURE_ANSWER
            try:
                request = consultation_request(question, core.corpus_version, max(1, int(remaining * 1000)))
            except ValueError:
                # Do not guess a user question from a malformed role transcript.
                failures += 1
                return question.id, INVALID_QUESTION_ANSWER
            try:
                result = await asyncio.wait_for(core.consult(request), timeout=remaining)
                core.validate_result(result)
                text = result.answer_text.strip()
                if not text or len(text) > MAX_ANSWER_CHARACTERS:
                    raise InvalidEvidence("Invalid output length")
                return question.id, text
            except (ConsultationUnavailable, InvalidEvidence, ValueError, TimeoutError):
                failures += 1
                return question.id, FAILURE_ANSWER

    # gather preserves input order, while every request receives fresh context IDs.
    answers = await asyncio.gather(*(answer(question) for question in questions))
    return list(answers), failures


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
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def environment_number(name: str, default: float, lower: float, upper: float, *, integer=False):
    value = os.getenv(name, "").strip()
    if not value or value == name:
        return int(default) if integer else default
    try:
        parsed = int(value) if integer else float(value)
    except ValueError as error:
        raise SubmissionError(f"{name} must be numeric") from error
    if not lower <= parsed <= upper:
        raise SubmissionError(f"{name} is outside the supported range")
    return parsed


def run_submission(data_dir: Path, output: Path, *, offline: bool = False) -> dict[str, int | str]:
    # Missing credentials are a configuration failure, never an implicit offline run.
    config = None if offline else LLMConfig.from_env(platform=True)
    questions = read_questions(data_dir / "questions.csv")
    workers = environment_number("MAX_WORKERS", 3, 1, 8, integer=True)
    case_timeout = environment_number("CONSULTATION_TIMEOUT_SECONDS", 60, 1, 120)
    total_timeout = environment_number("SUBMISSION_TIMEOUT_SECONDS", 1080, 1, 1140)
    with tempfile.TemporaryDirectory(prefix="energytool-submit-") as temporary:
        working = Path(temporary)
        document_count = extract_knowledge(data_dir / "knowledge-base.zip", working / "corpus")
        corpus = ingest(working / "corpus")
        corpus.save(working / "index")
        core = build_core(working / "index", llm_config=config)
        if not core.ready:
            raise SubmissionError("The supplied knowledge index is not ready")
        answers, failures = asyncio.run(answer_questions(
            questions, core, workers=workers,
            case_timeout_seconds=case_timeout, total_timeout_seconds=total_timeout,
        ))
        if [identifier for identifier, _ in answers] != [question.id for question in questions]:
            raise SubmissionError("Output IDs do not match the input")
        write_submission(output, answers)
    return {
        "questions": len(questions), "documents": document_count,
        "fallbacks": failures, "mode": "offline" if offline else "platform-model",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    parser.add_argument("--output", type=Path, default=Path("submission.csv"))
    parser.add_argument("--offline", action="store_true", help="Explicit local-only extractive baseline")
    args = parser.parse_args(argv)
    try:
        result = run_submission(args.data_dir, args.output, offline=args.offline)
    except SubmissionError as error:
        print(f"Submission input error: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, BadZipFile, csv.Error):
        # Provider credentials, document text and question content never enter logs.
        print("Submission failed: check input files, model environment and supported dependencies.", file=sys.stderr)
        return 2
    print(
        f"Wrote submission.csv: {result['questions']} answers; "
        f"{result['documents']} documents; mode={result['mode']}; "
        f"fallbacks={result['fallbacks']}.", file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
