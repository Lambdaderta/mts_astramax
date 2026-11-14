"""Official adapter checks using synthetic knowledge and a local fake proxy."""
from __future__ import annotations

import asyncio
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
from zipfile import ZipFile, ZipInfo

import pytest

from app.ml.protocol import ConsultationResult
from scripts.build_submission import build_submission, PLATFORM_REQUIREMENTS
import solution


SYNTHETIC_CLAIM = "Учебный маяк собирает показания учебных датчиков."
SYNTHETIC = (
    "Учебный маяк — вымышленный продукт для автоматического теста. "
    f"{SYNTHETIC_CLAIM} "
    "Учебные датчики передают показания один раз в сутки. "
    "Эти сведения не относятся к реальным продуктам Energytool."
)
MODEL_ENV = {
    "LLM_BASE_URL", "BASE_URL", "OPENAI_BASE_URL", "LLM_API_KEY", "OPENAI_API_KEY",
    "LLM_MODEL", "OPENAI_MODEL", "GEN_MODEL", "LLM_GENERATION_MODE",
    "TEMPERATURE", "MAX_TOKENS", "ML_TIMEOUT_SECONDS", "LLM_MAX_RETRIES",
    "DATA_DIR", "MAX_WORKERS", "CONSULTATION_TIMEOUT_SECONDS", "SUBMISSION_TIMEOUT_SECONDS",
    "PYTHONPATH",
    "RETRIEVAL_MODE", "TOP_K", "RETRIEVAL_CANDIDATES", "RRF_K", "LEXICAL_WEIGHT", "DENSE_WEIGHT",
    "RETRIEVAL_TIMEOUT_SECONDS", "EMBEDDING_CACHE_DIR", "EMB_MODEL", "EMBEDDER_API_KEY",
    "EMBEDDING_DIMENSIONS", "EMBEDDING_MODEL_REVISION", "EMBEDDING_BATCH_SIZE",
    "EMBEDDING_TIMEOUT_SECONDS", "EMBEDDING_MAX_RETRIES",
}


def write_questions(path, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "question"])
        writer.writerows(rows)


def make_data(path, rows=None):
    path.mkdir(parents=True)
    write_questions(path / "questions.csv", rows or [
        ("single", "Какие показания собирает учебный маяк?"),
        ("dialogue", "Пользователь: Интересует учебный маяк.\nАссистент: Какие сведения нужны?\nПользователь: Как передают показания учебные датчики?"),
    ])
    with ZipFile(path / "knowledge-base.zip", "w") as archive:
        archive.writestr("synthetic/learning.md", SYNTHETIC)
    return path


def csv_answers(path):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == ["id", "answer"]
        return list(reader)


def clean_env():
    return {key: value for key, value in os.environ.items() if key not in MODEL_ENV}


def test_input_rejects_duplicate_ids_and_empty_questions(tmp_path):
    path = tmp_path / "questions.csv"
    write_questions(path, [("a", "Вопрос"), (" a ", "Другой вопрос")])
    with pytest.raises(solution.SubmissionError, match="duplicate IDs"):
        solution.read_questions(path)
    write_questions(path, [("a", " ")])
    with pytest.raises(solution.SubmissionError, match="nonempty"):
        solution.read_questions(path)


def test_csv_round_trip_and_atomic_validation(tmp_path):
    output = tmp_path / "submission.csv"
    answer = 'Ответ с запятой, "кавычками"\nи новой строкой.'
    solution.write_submission(output, [("1", answer)])
    assert csv_answers(output) == [{"id": "1", "answer": answer}]
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    original = output.read_bytes()
    with pytest.raises(solution.SubmissionError):
        solution.write_submission(output, [("1", "x" * 10001)])
    assert output.read_bytes() == original
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert not list(tmp_path.glob("*.tmp"))


def test_dialogue_parser_preserves_latest_user_and_fresh_ids():
    question = solution.Question("case", "Пользователь: У нас учебный маяк.\nАссистент: Какая задача?\nПользователь: Нужны показания.\nАссистент: Эта устаревшая реплика не является последним вопросом.")
    first = solution.consultation_request(question, "synthetic", 1000)
    second = solution.consultation_request(question, "synthetic", 1000)
    assert first.latest_user_text == "Нужны показания."
    assert [message.role for message in first.messages] == ["user", "assistant", "user"]
    assert first.messages[-1].id == first.user_message_id
    assert first.session_id != second.session_id
    assert first.request_id != second.request_id
    assert {message.id for message in first.messages}.isdisjoint({message.id for message in second.messages})


def test_platform_dialogue_redacts_contact_values_across_history():
    question = solution.Question('case', (
        'Пользователь: Меня зовут Тестовый Клиент, телефон +7 (999) 000-00-00.\n'
        'Ассистент: Чем помочь, Тестовый Клиент?\n'
        'Пользователь: Измеряем расход для 12 объектов.'
    ))
    request = solution.consultation_request(question, 'synthetic', 1000)
    history = '\n'.join(message.text for message in request.messages)
    assert 'Тестовый Клиент' not in history
    assert '999' not in history
    assert '12 объектов' in request.latest_user_text


def test_cases_never_share_context_and_output_order_is_stable():
    class RecordingCore:
        corpus_version = "synthetic"

        def __init__(self):
            self.requests = []

        async def consult(self, request):
            self.requests.append(request)
            if "first" in request.latest_user_text:
                await asyncio.sleep(0.01)
            return ConsultationResult(kind="clarify", answer_text="Уточните задачу.", reason_code="test")

        def validate_result(self, result):
            pass

    core = RecordingCore()
    questions = [solution.Question("one", "first synthetic question"), solution.Question("two", "second synthetic question")]
    answers, failures = asyncio.run(solution.answer_questions(questions, core, workers=2))
    assert [identifier for identifier, _ in answers] == ["one", "two"]
    assert failures == 0
    assert len({request.session_id for request in core.requests}) == 2
    assert all(len(request.messages) == 1 for request in core.requests)


def test_unexpected_case_failure_preserves_all_output_ids_and_hides_error_body(tmp_path, capsys):
    class FailingCore:
        corpus_version = "synthetic"

        async def consult(self, request):
            if "first" in request.latest_user_text:
                raise RuntimeError("synthetic-secret-token and private-source-text")
            return ConsultationResult(kind="clarify", answer_text="Уточните задачу.", reason_code="test")

        def validate_result(self, result):
            pass

    questions = [solution.Question("0001", "first synthetic question"), solution.Question("a,b", "second synthetic question")]
    answers, failures = asyncio.run(solution.answer_questions(questions, FailingCore(), workers=1))
    output = tmp_path / "submission.csv"
    solution.write_submission(output, answers)
    assert failures == 1
    assert csv_answers(output) == [
        {"id": "0001", "answer": solution.FAILURE_ANSWER},
        {"id": "a,b", "answer": "Уточните задачу."},
    ]
    captured = capsys.readouterr()
    assert "phase=case_failed; case_index=1; exception_type=RuntimeError" in captured.err
    assert "synthetic-secret-token" not in captured.out + captured.err
    assert "private-source-text" not in captured.out + captured.err


def test_case_cancellation_is_not_swallowed_as_a_fallback():
    class CancelledCore:
        corpus_version = "synthetic"

        async def consult(self, request):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(solution.answer_questions([solution.Question("one", "Synthetic question")], CancelledCore()))


def test_submission_budget_cancels_active_cases_and_leaves_fallbacks_for_queued_cases():
    class BlockingCore:
        corpus_version = 'synthetic'

        def __init__(self):
            self.active = 0
            self.calls = 0

        async def consult(self, request):
            self.calls += 1
            self.active += 1
            try:
                await asyncio.Event().wait()
            finally:
                self.active -= 1

    core = BlockingCore()
    questions = [solution.Question(str(index), 'Учебный вопрос') for index in range(12)]
    answers, failures = asyncio.run(solution.answer_questions(
        questions, core, workers=2, total_timeout_seconds=0.05,
    ))
    assert answers == [(question.id, solution.FAILURE_ANSWER) for question in questions]
    assert failures == len(questions)
    assert core.calls == 2
    assert core.active == 0


def test_platform_adapter_does_not_truncate_long_last_question():
    question = solution.Question("long", "я" * 4001)
    request = solution.consultation_request(question, "synthetic", 1000)
    assert request.latest_user_text == question.text


def test_malformed_dialogue_has_explicit_fallback():
    class NeverCalled:
        corpus_version = "synthetic"

        async def consult(self, request):
            raise AssertionError("Invalid dialogue should not reach the model")

    answers, failures = asyncio.run(solution.answer_questions([solution.Question("bad", "Ассистент: Только ответ без вопроса.")], NeverCalled()))
    assert answers == [("bad", solution.INVALID_QUESTION_ANSWER)]
    assert failures == 1


def test_direct_manager_request_skips_model_without_claiming_lead_was_saved():
    class NeverCalled:
        corpus_version = 'synthetic'

        async def consult(self, request):
            raise AssertionError('A direct manager request must bypass the model')

    question = solution.Question('handoff', 'Соедините меня с менеджером, пожалуйста.')
    answers, failures = asyncio.run(solution.answer_questions([question], NeverCalled()))
    assert answers == [('handoff', solution.CONTACT_REQUEST_ANSWER)]
    assert 'телефон' in answers[0][1] and 'сохран' not in answers[0][1]
    assert failures == 0


@pytest.mark.parametrize("name", ["../escaped.md", "/absolute.md", "nested/../../escaped.md", "nested\\escaped.md", "C:/escaped.md"])
def test_archive_rejects_traversal_before_extracting(tmp_path, name):
    archive = tmp_path / "knowledge-base.zip"
    with ZipFile(archive, "w") as source:
        source.writestr(name, SYNTHETIC)
    with pytest.raises(solution.SubmissionError, match="unsafe path"):
        solution.extract_knowledge(archive, tmp_path / "corpus")
    assert not (tmp_path / "escaped.md").exists()


def test_archive_rejects_symlink_and_oversized_member(tmp_path, monkeypatch):
    archive = tmp_path / "knowledge-base.zip"
    link = ZipInfo("link.md")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive, "w") as source:
        source.writestr(link, "../../elsewhere")
    with pytest.raises(solution.SubmissionError, match="link or special file"):
        solution.extract_knowledge(archive, tmp_path / "symlink-corpus")
    with ZipFile(archive, "w") as source:
        source.writestr("large.md", SYNTHETIC)
    monkeypatch.setattr(solution, "MAX_MEMBER_BYTES", 10)
    with pytest.raises(solution.SubmissionError, match="32 MiB"):
        solution.extract_knowledge(archive, tmp_path / "large-corpus")


def test_archive_rejects_duplicate_paths(tmp_path):
    archive = tmp_path / "knowledge-base.zip"
    with ZipFile(archive, "w") as source:
        source.writestr("source.md", SYNTHETIC)
        source.writestr("SOURCE.md", SYNTHETIC)
    with pytest.raises(solution.SubmissionError, match="duplicate document paths"):
        solution.extract_knowledge(archive, tmp_path / "corpus")


def test_missing_model_configuration_does_not_silently_run_offline(tmp_path, monkeypatch):
    for name in MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    data = make_data(tmp_path / "data")
    output = tmp_path / "submission.csv"
    with pytest.raises(ValueError):
        solution.run_submission(data, output)
    assert not output.exists()


def test_packaging_is_root_only_deterministic_and_runs_without_app_package(tmp_path):
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    names = build_submission(first)
    build_submission(second)
    assert first.read_bytes() == second.read_bytes()
    assert all("/" not in name and "\\" not in name for name in names)
    assert {"solution.py", "requirements.txt"}.issubset(names)
    assert all(name.endswith(".py") or name == "requirements.txt" for name in names)
    runtime = tmp_path / "runtime"
    with ZipFile(first) as archive:
        assert archive.read("requirements.txt").decode() == PLATFORM_REQUIREMENTS
        for name in archive.namelist():
            if name.endswith(".py"):
                source = archive.read(name).decode()
                compile(source, name, "exec")
                assert "from app." not in source
        archive.extractall(runtime)  # Our own, validated deterministic package.
    data = make_data(tmp_path / "data")
    env = clean_env()
    env["DATA_DIR"] = str(data)
    process = subprocess.run([sys.executable, "solution.py", "--offline"], cwd=runtime, env=env, text=True, capture_output=True, timeout=20)
    assert process.returncode == 0, process.stderr
    assert "mode=offline" in process.stderr
    assert [row["id"] for row in csv_answers(runtime / "submission.csv")] == ["single", "dialogue"]
    assert not (runtime / "app").exists()


@pytest.mark.parametrize(("placement", "questions_name", "knowledge_name"), [
    ("data_dir", "questions.csv", "knowledge-base.zip"),
    ("data_dir", "train.csv", "knowledge_base.zip"),
    ("script_relative", "questions.csv", "kb.zip"),
    ("cwd", "train.csv", "knowledge-base.zip"),
])
def test_packaged_input_lookup_matches_official_sample(tmp_path, placement, questions_name, knowledge_name):
    package_path = tmp_path / "submission.zip"
    build_submission(package_path)
    runtime = tmp_path / "runtime"
    with ZipFile(package_path) as package:
        package.extractall(runtime)
    working = tmp_path / "working"
    working.mkdir()
    roots = {"data_dir": working / "inputs", "script_relative": runtime / "inputs", "cwd": working}
    data = roots[placement]
    data.mkdir(parents=True, exist_ok=True)
    write_questions(data / questions_name, [("0001", "Соедините меня с менеджером, пожалуйста.")])
    with ZipFile(data / knowledge_name, "w") as archive:
        archive.writestr("synthetic.md", SYNTHETIC)
    env = {**clean_env(), "DATA_DIR": "inputs"}
    process = subprocess.run(
        [sys.executable, str(runtime / "solution.py"), "--offline"],
        cwd=working, env=env, capture_output=True, text=True, timeout=20,
    )
    assert process.returncode == 0, process.stderr
    assert csv_answers(working / "submission.csv") == [{"id": "0001", "answer": solution.CONTACT_REQUEST_ANSWER}]
    assert not (runtime / "submission.csv").exists()


def test_input_lookup_prioritizes_data_dir_and_documented_names(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    working = tmp_path / "working"
    working.mkdir()
    for name in ("questions.csv", "train.csv"):
        (data / name).write_text("synthetic", encoding="utf-8")
        (working / name).write_text("synthetic", encoding="utf-8")
    monkeypatch.chdir(working)
    assert solution.find_input_file(data, "questions.csv", "train.csv") == data / "questions.csv"
    (data / "questions.csv").unlink()
    assert solution.find_input_file(data, "questions.csv", "train.csv") == data / "train.csv"


def test_platform_case_budget_defaults_to_120_seconds(tmp_path, monkeypatch):
    data = make_data(tmp_path / "data")
    captured = {}

    async def capture(questions, core, **kwargs):
        captured.update(kwargs)
        return [(question.id, "Уточните задачу.") for question in questions], 0

    for name in MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONSULTATION_TIMEOUT_SECONDS", "CONSULTATION_TIMEOUT_SECONDS")
    monkeypatch.setattr(solution, "answer_questions", capture)
    solution.run_submission(data, tmp_path / "submission.csv", offline=True)
    assert captured["case_timeout_seconds"] == 120
    assert 1070 < captured["total_timeout_seconds"] <= 1080


@pytest.fixture
def fake_proxy():
    requests = []
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            embedding_query_failure = state.get("embedding_query_failure") and self.path.endswith("/embeddings") and sum(
                item["path"].endswith("/embeddings") for item in requests
            ) > 1
            if state["status"] != 200 or embedding_query_failure:
                self.send_response(401 if embedding_query_failure else state["status"])
                self.end_headers()
                self.wfile.write(b"synthetic provider error, never expose this")
                return
            if self.path.endswith("/embeddings"):
                payload = {"data": [{"index": i, "embedding": [1.0, 0.5, 0.1]} for i, _ in enumerate(body["input"])]}
            else:
                supplied = json.loads(body["messages"][-1]["content"])
                sources = supplied["SOURCE"]
                if sources:
                    model = {"kind": "answer", "answer_text": SYNTHETIC_CLAIM, "chunk_ids": [sources[0]["chunk_id"]]}
                else:
                    model = {"kind": "insufficient_knowledge", "answer_text": "Нет сведений.", "chunk_ids": []}
                payload = {"choices": [{"message": {"content": json.dumps(model, ensure_ascii=False)}}]}
            response = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_packaged_official_csv_adapter_uses_model_aliases_and_independent_dialogues(tmp_path, fake_proxy):
    base_url, calls, _ = fake_proxy
    archive = tmp_path / "submission.zip"
    build_submission(archive)
    runtime = tmp_path / "runtime"
    with ZipFile(archive) as package:
        package.extractall(runtime)
    data = make_data(tmp_path / "data")
    env = clean_env()
    env.update({
        "DATA_DIR": str(data), "OPENAI_BASE_URL": base_url,
        "OPENAI_API_KEY": "synthetic-test-key", "GEN_MODEL": "synthetic-test-model",
        "TEMPERATURE": "0.2", "MAX_TOKENS": "500", "MAX_WORKERS": "2",
        "EMB_MODEL": "synthetic-embedding-model",
    })
    process = subprocess.run([sys.executable, "solution.py"], cwd=runtime, env=env, text=True, capture_output=True, timeout=20)
    assert process.returncode == 0, process.stderr
    assert "mode=platform-model" in process.stderr
    assert "fallbacks=0" in process.stderr
    assert "retrieval=hybrid" in process.stderr
    assert "retrieval_fallback=none" in process.stderr
    assert "retrieval_counts=hybrid:2" in process.stderr
    rows = csv_answers(runtime / "submission.csv")
    assert [row["id"] for row in rows] == ["single", "dialogue"]
    assert all(row["answer"] == SYNTHETIC_CLAIM for row in rows)
    assert all(call["auth"] == "Bearer synthetic-test-key" for call in calls)
    embedding_calls = [call for call in calls if call["path"] == "/v1/embeddings"]
    generation_calls = [call for call in calls if call["path"] == "/v1/chat/completions"]
    assert len(embedding_calls) == 3  # One shared corpus build and two distinct questions.
    assert len(generation_calls) == 2
    assert all(call["body"]["model"] == "synthetic-embedding-model" for call in embedding_calls)
    assert all(call["body"]["model"] == "synthetic-test-model" for call in generation_calls)
    assert all(call["body"]["temperature"] == 0.2 and call["body"]["max_tokens"] == 500 for call in generation_calls)
    dialogues = [json.loads(call["body"]["messages"][-1]["content"])["dialogue"] for call in generation_calls]
    assert sorted(len(dialogue) for dialogue in dialogues) == [1, 3]
    assert "synthetic-test-key" not in process.stderr + process.stdout


@pytest.mark.parametrize("url_alias", ["BASE_URL", "OPENAI_BASE_URL"])
def test_packaged_official_placeholders_use_aliases_and_defaults(tmp_path, fake_proxy, url_alias):
    base_url, calls, _ = fake_proxy
    package_path = tmp_path / "submission.zip"
    build_submission(package_path)
    runtime = tmp_path / "runtime"
    with ZipFile(package_path) as package:
        package.extractall(runtime)
    make_data(runtime / "data")
    # The organizer sample explicitly treats literal template names as unset.
    env = {**clean_env(), **{name: name for name in MODEL_ENV if name != "PYTHONPATH"}}
    env.update({url_alias: "  " + base_url + "  ", "OPENAI_API_KEY": "synthetic-test-key", "GEN_MODEL": "synthetic-generation"})
    process = subprocess.run(
        [sys.executable, "solution.py"], cwd=runtime, env=env, capture_output=True, text=True, timeout=20,
    )
    assert process.returncode == 0, process.stderr
    assert [row["id"] for row in csv_answers(runtime / "submission.csv")] == ["single", "dialogue"]
    assert "fallbacks=0" in process.stderr
    assert "retrieval_counts=hybrid:2" in process.stderr
    embedding_calls = [call for call in calls if call["path"].endswith("/embeddings")]
    generation_calls = [call for call in calls if call["path"].endswith("/chat/completions")]
    assert len(embedding_calls) == 3 and len(generation_calls) == 2
    assert all(call["auth"] == "Bearer synthetic-test-key" for call in calls)
    assert all(call["body"]["model"] == "baai/bge-m3" for call in embedding_calls)
    assert all(call["body"]["model"] == "synthetic-generation" for call in generation_calls)
    assert all(call["body"]["temperature"] == 0 and call["body"]["max_tokens"] == 2048 for call in generation_calls)
    assert all(marker in process.stderr for marker in (
        "Submission started", "Submission inputs ready", "Submission retrieval ready", "Submission generation complete", "Wrote submission.csv",
    ))
    assert all(value not in process.stderr + process.stdout for value in ("synthetic-test-key", base_url, SYNTHETIC_CLAIM))


def test_provider_failure_still_emits_every_id_without_false_product_claim(tmp_path, fake_proxy, monkeypatch):
    base_url, calls, state = fake_proxy
    state["status"] = 401
    for name in MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", base_url)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("LLM_MODEL", "synthetic-test-model")
    data = make_data(tmp_path / "data")
    output = tmp_path / "submission.csv"
    result = solution.run_submission(data, output)
    assert result["fallbacks"] == 2
    assert [row["id"] for row in csv_answers(output)] == ["single", "dialogue"]
    assert all(row["answer"] == solution.FAILURE_ANSWER for row in csv_answers(output))
    assert result["retrieval"] == "lexical_fallback"
    assert result["retrieval_fallback"] == "embedding_http_error"
    assert len(calls) == 3  # One failed corpus build, then two generation attempts; no auth retries.


def test_query_embedding_failures_are_not_reported_as_successful_hybrid(tmp_path, fake_proxy, monkeypatch):
    base_url, _, state = fake_proxy
    state["embedding_query_failure"] = True
    for name in MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", base_url)
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("LLM_MODEL", "synthetic-test-model")
    data = make_data(tmp_path / "data")
    output = tmp_path / "submission.csv"
    result = solution.run_submission(data, output)
    assert result["fallbacks"] == 0  # Grounded generation still succeeds using BM25.
    assert result["configured_retrieval"] == "hybrid"
    assert result["retrieval"] == "lexical_fallback"
    assert result["retrieval_fallback"] == "none"  # Corpus warm-up itself succeeded.
    assert result["retrieval_counts"] == "lexical_fallback:2"
    assert all(row["answer"] == SYNTHETIC_CLAIM for row in csv_answers(output))
