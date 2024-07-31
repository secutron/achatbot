import time
import atexit
import logging
import multiprocessing
import os
import argparse
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from src.common.types import DailyRoomBotArgs
from src.common.logger import Logger
from src.common.interface import IBot
from src.services.help.daily_rest import DailyRESTHelper, \
    DailyRoomObject, DailyRoomProperties, DailyRoomParams
from src.cmd.bots.base import register_daily_room_bots

from dotenv import load_dotenv
load_dotenv(override=True)


Logger.init(logging.DEBUG, is_file=False, is_console=True)


# --------------------- API -----------------
class BotInfo(BaseModel):
    is_agent: bool = False
    chat_bot_name: str = "DailyRTVIBot"
    bot_config: dict = {}
    room_name: str = "chat-room"


class APIResponse(BaseModel):
    error_code: int = 0
    error_detail: str = ""
    data: Any | None = None


# biz error code
ERROR_CODE_NO_ROOM = 10000

# ------------------ daily room --------------------------

DAILY_API_URL = os.getenv("DAILY_API_URL", "https://api.daily.co/v1")
DAILY_API_KEY = os.getenv("DAILY_API_KEY", "")
ROOM_EXPIRE_TIME = 30 * 60  # 30 minutes
ROOM_TOKEN_EXPIRE_TIME = 30 * 60  # 30 minutes

# --------------------- Bot ----------------------------


MAX_BOTS_PER_ROOM = 10


# Bot sub-process dict for status reporting and concurrency control
bot_procs = {}


def get_room_bot_proces_num(room_name):
    num = 0
    for val in bot_procs.values():
        proc: multiprocessing.Process = val[0]
        room: DailyRoomObject = val[1]
        if room.name == room_name and proc.is_alive():
            num += 1
    return num


def cleanup():
    # Clean up function, just to be extra safe
    for pid, (proc, room) in bot_procs.items():
        if proc.is_alive():
            proc.join()
            proc.terminate()
            proc.close()
            logging.info(f"pid:{pid} room:{room.name} proc: {proc} close")
        else:
            logging.warning(f"pid:{pid} room:{room.name} proc: {proc} already closed")


atexit.register(cleanup)

# ------------ Fast API Config ------------ #

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------ Helper methods ------------ #


def escape_bash_arg(s):
    return "'" + s.replace("'", "'\\''") + "'"


def check_host_whitelist(request: Request):
    host_whitelist = os.getenv("HOST_WHITELIST", "")
    request_host_url = request.headers.get("host")

    if not host_whitelist:
        return True

    # Split host whitelist by comma
    allowed_hosts = host_whitelist.split(",")

    # Return True if no whitelist exists are specified
    if len(allowed_hosts) < 1:
        return True

    # Check for apex and www variants
    if any(domain in allowed_hosts for domain in [request_host_url, f"www.{request_host_url}"]):
        return True

    return False


# ------------ Fast API Routes ------------ #
# https://fastapi.tiangolo.com/async/

@app.middleware("http")
async def allowed_hosts_middleware(request: Request, call_next):
    # Middle that optionally checks for hosts in a whitelist
    if not check_host_whitelist(request):
        raise HTTPException(status_code=403, detail="Host access denied")
    response = await call_next(request)
    return response


@app.get("/create_room/{name}")
def create_room(name):
    # Create a Daily rest helper
    daily_rest_helper = DailyRESTHelper(DAILY_API_KEY, DAILY_API_URL)
    # Create a new room
    try:
        params = DailyRoomParams(name=name, properties=DailyRoomProperties(
            exp=time.time() + ROOM_EXPIRE_TIME,
        ))
        room: DailyRoomObject = daily_rest_helper.create_room(params=params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{e}")

    return RedirectResponse(room.url)


"""
curl -XPOST "http://0.0.0.0:4321/bot_join" \
    -H "Content-Type: application/json" \
    -d $'{"chat_bot_name":"DummyBot","room_name":"chat-bot","bot_config":{"llm":{"messages":[{"role":"system","content":"你是聊天机器人，一个友好、乐于助人的机器人。您的输出将被转换为音频，所以不要包含除“!”以外的特殊字符。'或'?的答案。以一种创造性和有用的方式回应用户 所说的话，但要保持简短。从打招呼开始。"}]},"tts":{"voice":"e90c6678-f0d3-4767-9883-5d0ecf5894a8"}}}' | jq .
"""


@app.post("/bot_join")
async def bot_join(info: BotInfo) -> JSONResponse:
    logging.info(f"request bot info: {info}")

    num_bots_in_room = get_room_bot_proces_num(info.room_name)
    logging.info(f"num_bots_in_room: {num_bots_in_room}")
    if num_bots_in_room >= MAX_BOTS_PER_ROOM:
        raise HTTPException(
            status_code=500, detail=f"Max bot limited reach for room: {info.room_name}")

    logging.info(f"register bots: {register_daily_room_bots.items()}")
    if info.chat_bot_name not in register_daily_room_bots:
        raise HTTPException(status_code=500, detail=f"bot {info.chat_bot_name} don't exist")

    # Create a Daily rest helper
    daily_rest_helper = DailyRESTHelper(DAILY_API_KEY, DAILY_API_URL)
    room: DailyRoomObject | None = None
    try:
        room = daily_rest_helper.get_room_from_name(info.room_name)
    except Exception as ex:
        logging.warning(f"Failed to get room {info.room_name} from Daily REST API: {ex}")
        # Create a new room
        try:
            params = DailyRoomParams(
                name=info.room_name,
                properties=DailyRoomProperties(
                    exp=time.time() + ROOM_EXPIRE_TIME,
                ),
            )
            room = daily_rest_helper.create_room(params=params)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{e}")

    # Give the agent a token to join the session
    token = daily_rest_helper.get_token(room.url, ROOM_TOKEN_EXPIRE_TIME)

    if not room or not token:
        raise HTTPException(
            status_code=500, detail=f"Failed to get token for room: {room.name}")

    logging.info(f"room: {room}")
    pid = 0
    try:
        kwargs = DailyRoomBotArgs(
            bot_config=info.bot_config,
            room_url=room.url,
            token=token,
            bot_name=info.chat_bot_name,
        ).__dict__
        bot_obj: IBot = register_daily_room_bots[info.chat_bot_name](**kwargs)
        bot_process: multiprocessing.Process = multiprocessing.Process(
            target=bot_obj.run,
            name=info.chat_bot_name)
        bot_process.start()
        pid = bot_process.pid
        bot_procs[pid] = (bot_process, room)

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"bot {info.chat_bot_name} failed to start process: {e}")

    # Grab a token for the user to join with
    user_token = daily_rest_helper.get_token(room.url, ROOM_TOKEN_EXPIRE_TIME)

    return JSONResponse({
        "room_name": room.name,
        "room_url": room.url,
        "token": user_token,
        "bot_config": bot_obj.bot_config(),
        "bot_id": pid,
        "status": "running",
    })


"""
curl -XGET "http://0.0.0.0:4321/status/53187" | jq .
"""


@ app.get("/status/{pid}")
def get_status(pid: int):
    # Look up the subprocess
    val = bot_procs.get(pid)
    if val is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    proc: multiprocessing.Process = val[0]
    room: DailyRoomObject = val[1]
    # If the subprocess doesn't exist, return an error
    if not proc:
        raise HTTPException(
            status_code=404, detail=f"Bot with process id: {pid} not found")

    # Check the status of the subprocess
    if proc.is_alive():
        status = "running"
    else:
        status = "finished"

    return JSONResponse({
        "bot_id": pid,
        "status": status,
        "room_info": room.model_dump(),
    })


"""
curl -XGET "http://0.0.0.0:4321/room/num_bots/chat-bot" | jq .
"""


@app.get("/room/num_bots/{room_name}")
def get_status(room_name: str):
    return JSONResponse({
        "num_bots": get_room_bot_proces_num(room_name),
    })


"""
curl -XGET "http://0.0.0.0:4321/room/bots/chat-bot" | jq .
"""


@app.get("/room/bots/{room_name}")
def get_status(room_name: str):
    procs = []
    _room = None
    for val in bot_procs.values():
        proc: multiprocessing.Process = val[0]
        room: DailyRoomObject = val[1]
        if room.name == room_name:
            procs.append({
                "pid": proc.pid,
                "name": proc.name,
                "status": "running" if proc.is_alive() else "finished",
            })
            _room = room

    if _room is None:
        daily_rest_helper = DailyRESTHelper(DAILY_API_KEY, DAILY_API_URL)
        try:
            _room = daily_rest_helper.get_room_from_name(room_name)
        except Exception as ex:
            return JSONResponse(
                APIResponse(
                    error_code=ERROR_CODE_NO_ROOM,
                    error_detail=f"Failed to get room {room_name} from Daily REST API: {ex}",
                    data=None,
                ).model_dump()
            )

    response = APIResponse(
        error_code=0,
        error_detail="",
        data={
            "room_info": _room.model_dump(),
            "bots": procs,
        },
    )

    return JSONResponse(response.model_dump())


if __name__ == "__main__":
    import uvicorn

    default_host = os.getenv("HOST", "0.0.0.0")
    default_port = int(os.getenv("FAST_API_PORT", "4321"))

    parser = argparse.ArgumentParser(
        description="RTVI Bot Runner")
    parser.add_argument("--host", type=str,
                        default=default_host, help="Host address")
    parser.add_argument("--port", type=int,
                        default=default_port, help="Port number")
    parser.add_argument("--reload", action="store_true",
                        help="Reload code on change")

    config = parser.parse_args()

    uvicorn.run(
        "src.cmd.http.server.fastapi_daily_bot_serve:app",
        host=config.host,
        port=config.port,
        reload=config.reload
    )
