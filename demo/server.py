"""Minimal A2A "Hello World" agent (a2a-sdk 1.x). No LLM, no API keys.

Behaviour: greets back and reverses the text you send.
Agent Card: http://127.0.0.1:9999/.well-known/agent-card.json
JSON-RPC endpoint: http://127.0.0.1:9999/
"""
import uvicorn
from starlette.applications import Starlette

from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)

HOST, PORT = "127.0.0.1", 9999


def respond(text: str) -> str:
    """The 'agent logic': deterministic, no model needed."""
    return f"Hello! You said '{text}'. Reversed: '{text[::-1]}'"


class HelloExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if not task:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Working on it..."),
        )

        query = get_message_text(context.message) or ""
        result = respond(query) if query else "No text input was provided."

        await updater.add_artifact(parts=[new_text_part(text=result, media_type="text/plain")])
        await updater.update_status(
            state=TaskState.TASK_STATE_COMPLETED,
            message=new_text_message("Done."),
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancel is not supported.")


skill = AgentSkill(
    id="greet_and_reverse",
    name="Greet and reverse",
    description="Greets you and returns your text reversed.",
    input_modes=["text/plain"],
    output_modes=["text/plain"],
    tags=["demo", "hello-world"],
    examples=["hello A2A"],
)

agent_card = AgentCard(
    name="Hello Reverse Agent",
    description="A minimal A2A demo agent that greets and reverses text.",
    version="0.1.0",
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    capabilities=AgentCapabilities(streaming=True),
    supported_interfaces=[
        AgentInterface(protocol_binding="JSONRPC", url=f"http://{HOST}:{PORT}", protocol_version="1.0")
    ],
    skills=[skill],
)

handler = DefaultRequestHandler(
    agent_executor=HelloExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=agent_card,
)

routes = []
routes.extend(create_agent_card_routes(agent_card))
routes.extend(create_jsonrpc_routes(handler, "/"))
app = Starlette(routes=routes)

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
