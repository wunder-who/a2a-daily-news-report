"""A2A client demo: fetch the Agent Card, send a message, then stream one."""
import asyncio
import sys

import httpx
from google.protobuf.json_format import MessageToJson

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types import Role, SendMessageRequest

BASE_URL = "http://127.0.0.1:9999"


def show(obj) -> str:
    try:
        return MessageToJson(obj, indent=2)
    except Exception:
        return repr(obj)


async def main(text: str) -> None:
    # 1. Discover the agent via its Agent Card
    async with httpx.AsyncClient() as http:
        card = await A2ACardResolver(httpx_client=http, base_url=BASE_URL).get_agent_card()
    print("=== Agent Card ===")
    print(show(card))

    # 2. Non-streaming call
    print("\n=== send_message (non-streaming) ===")
    client = await create_client(agent=card, client_config=ClientConfig(streaming=False))
    req = SendMessageRequest(message=new_text_message(text, role=Role.ROLE_USER))
    async for chunk in client.send_message(req):
        print(show(chunk))
    await client.close()

    # 3. Streaming call: prints each event as the server emits it
    print("\n=== send_message (streaming) ===")
    client = await create_client(agent=card, client_config=ClientConfig(streaming=True))
    req = SendMessageRequest(message=new_text_message(text, role=Role.ROLE_USER))
    n = 0
    async for chunk in client.send_message(req):
        n += 1
        print(f"--- event {n} ---")
        print(show(chunk))
    await client.close()


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "hello A2A"))
