"""Step 29 离线问答契约、卡片和信号隔离；禁止实际网络。"""

from dataclasses import replace
from uuid import uuid4

import pytest
from temporalio.converter import DataConverter

from app.agent.investigation import MainAgent
from app.agent.models import ChatRequest
from app.agent.scenario import payment_llm
from app.connectors.feishu.fake import FakeFeishuConnector
from app.tasks.human.activities import question_card
from app.tasks.human.models import question_id, validate_answer
from app.tasks.states import TaskStatus
from app.tasks.workflow import AITaskWorkflow, validate_workflow_input
from app.tasks.workflow_models import (
    HumanAnswer,
    HumanAnswerRequest,
    HumanAnswerResult,
    HumanPrompt,
    HumanQuestion,
    HumanResponse,
    HumanWaitRequest,
    TaskSnapshot,
    WorkflowInput,
)
from tests.test_main_agent import SPEC, MemoryIO

pytestmark = pytest.mark.usefixtures("forbid_llm_network")


def prompt(status: TaskStatus = TaskStatus.NEED_HUMAN_JUDGMENT) -> HumanPrompt:
    task = TaskSnapshot(str(uuid4()), status, 4)
    return HumanPrompt(
        task,
        question_id(task),
        str(uuid4()),
        "高峰期优先稳定性还是成本？",
        TaskStatus.INVESTIGATING,
    )


def answer(value: HumanPrompt) -> HumanAnswer:
    return HumanAnswer(
        value.question_id, value.task.status, value.task.version, "优先稳定性", "owner"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"question_id": "invalid"},
        {"question_id": str(uuid4()).upper()},
        {"wait_status": TaskStatus.WAITING_APPROVAL},
        {"wait_version": 0},
        {"wait_version": True},
        {"answer": ""},
        {"answer": " \n"},
        {"answer": "x" * 8001},
        {"respondent": " "},
        {"respondent": "x" * 201},
    ],
)
def test_invalid_answer(changes: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        validate_answer(replace(answer(prompt()), **changes))  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [TaskStatus.WAITING_INFORMATION, TaskStatus.NEED_HUMAN_JUDGMENT])
@pytest.mark.asyncio
async def test_card_identity_and_fake_delivery(status: TaskStatus) -> None:
    value = prompt(status)
    card = question_card(HumanWaitRequest(value.task, value.question, value.resume_status))
    assert str(card.notification_id) == value.question_id
    assert card.card.buttons[0].value["wait_version"] == 4
    assert card.card.buttons[0].value["operation"] == "human_answer"
    assert value.question in card.card.markdown and value.task.task_id in card.card.markdown
    connector = FakeFeishuConnector()
    first = await connector.send(card)
    assert await connector.send(card) == first
    assert connector.get_sent(card.notification_id).notification == card
    assert len(connector.sent_messages) == 1
    assert question_id(replace(value.task, version=5)) != value.question_id


@pytest.mark.parametrize("question", ["", " \t", "x" * 3001])
def test_invalid_question(question: str) -> None:
    with pytest.raises(ValueError):
        validate_workflow_input(
            WorkflowInput(
                str(uuid4()),
                human_questions=[HumanQuestion(TaskStatus.WAITING_INFORMATION, question)],
            )
        )


@pytest.mark.parametrize(
    "questions,waits",
    [
        ([HumanQuestion(TaskStatus.WAITING_APPROVAL, "审批")], []),
        ([HumanQuestion(TaskStatus.WAITING_INFORMATION, "问题")] * 2, []),
        ([HumanQuestion(TaskStatus.WAITING_INFORMATION, "问题")], [TaskStatus.WAITING_INFORMATION]),
    ],
)
def test_wait_contract_excludes_approval_and_placeholders(
    questions: list[HumanQuestion], waits: list[TaskStatus]
) -> None:
    with pytest.raises(ValueError):
        validate_workflow_input(WorkflowInput(str(uuid4()), waits=waits, human_questions=questions))


@pytest.mark.parametrize(
    "changes",
    [
        {"question_id": str(uuid4())},
        {"wait_status": TaskStatus.WAITING_INFORMATION},
        {"wait_version": 3},
        {"wait_version": True},
        {"answer": " "},
        {"respondent": " "},
    ],
)
def test_signal_ignores_wrong_or_invalid_answer(changes: dict[str, object]) -> None:
    value = prompt()
    instance = AITaskWorkflow()
    instance.task, instance.human_prompt, instance.human_wait_active = value.task, value, True
    instance.answer_question(replace(answer(value), **changes))  # type: ignore[arg-type]
    assert instance.human_answer is None


def test_first_answer_wins_and_boolean_cannot_resume() -> None:
    value = prompt()
    instance = AITaskWorkflow()
    instance.task, instance.human_prompt, instance.human_wait_active = value.task, value, True
    response = answer(value)
    instance.human_response(HumanResponse(value.task.status, 4, True))
    assert instance.response is None
    instance.answer_question(response)
    instance.answer_question(replace(response, answer="其他回答"))
    assert instance.human_answer == response
    instance.human_answer = None
    instance.human_wait_active = False
    instance.answer_question(response)
    assert instance.human_answer is None


@pytest.mark.asyncio
async def test_temporal_payload_roundtrip() -> None:
    value = prompt()
    converter = DataConverter.default
    for item in [
        value,
        answer(value),
        HumanAnswerRequest(value, answer(value)),
        HumanAnswerResult(str(uuid4()), str(uuid4())),
        WorkflowInput(
            value.task.task_id, human_questions=[HumanQuestion(value.task.status, value.question)]
        ),
    ]:
        assert (await converter.decode(await converter.encode([item]), [type(item)]))[0] == item


def test_card_cannot_resume_execution() -> None:
    value = prompt()
    with pytest.raises(ValueError):
        question_card(HumanWaitRequest(value.task, value.question, TaskStatus.EXECUTING))


@pytest.mark.asyncio
async def test_human_context_reaches_agent_without_granting_tool_evidence() -> None:
    llm = payment_llm()
    io = MemoryIO(llm)
    context = '{"answer":"高峰期优先稳定性"}'
    result = await MainAgent().run(SPEC, io, human_context=context)
    first = llm.calls[0]
    assert isinstance(first, ChatRequest)
    assert any(context in (message.content or "") for message in first.messages)
    assert result.observed_ids == io.ids and len(io.ids) == 4
