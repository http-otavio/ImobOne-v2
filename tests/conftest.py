import sys
import types
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def _make_async_mock(**kwargs):
    m = AsyncMock()
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _stub_module(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _ensure_parent(dotted: str):
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            sys.modules[parent] = types.ModuleType(parent)


# ---------------------------------------------------------------------------
# Stub all heavy / external dependencies before any production module loads
# ---------------------------------------------------------------------------

# supabase
_stub_module("supabase")
_stub_module("supabase.client")

# anthropic
anthropic_mod = _stub_module("anthropic")
anthropic_mod.Anthropic = MagicMock
anthropic_mod.AsyncAnthropic = MagicMock

# openai
openai_mod = _stub_module("openai")
openai_mod.OpenAI = MagicMock
openai_mod.AsyncOpenAI = MagicMock

# redis
redis_mod = _stub_module("redis")
redis_mod.Redis = MagicMock
redis_asyncio_mod = _stub_module("redis.asyncio")
redis_asyncio_mod.Redis = MagicMock
redis_asyncio_mod.from_url = MagicMock(return_value=MagicMock())

# httpx
httpx_mod = _stub_module("httpx")
httpx_mod.AsyncClient = MagicMock
httpx_mod.Client = MagicMock
httpx_mod.HTTPStatusError = Exception
httpx_mod.RequestError = Exception

# fastapi
fastapi_mod = _stub_module("fastapi")
fastapi_mod.FastAPI = MagicMock
fastapi_mod.APIRouter = MagicMock
fastapi_mod.Request = MagicMock
fastapi_mod.Response = MagicMock
fastapi_mod.HTTPException = Exception
fastapi_mod.Depends = MagicMock(return_value=None)
fastapi_mod.BackgroundTasks = MagicMock

_stub_module("fastapi.responses")
_stub_module("fastapi.middleware")
_stub_module("fastapi.middleware.cors")

# pydantic
pydantic_mod = _stub_module("pydantic")


class _BaseModel:
    def __init__(self, **data):
        for k, v in data.items():
            setattr(self, k, v)

    def dict(self):
        return self.__dict__.copy()

    def model_dump(self):
        return self.__dict__.copy()


pydantic_mod.BaseModel = _BaseModel
pydantic_mod.Field = MagicMock(return_value=None)
pydantic_mod.validator = MagicMock(return_value=lambda f: f)
pydantic_mod.field_validator = MagicMock(return_value=lambda f: f)

# celery
celery_mod = _stub_module("celery")
celery_mod.Celery = MagicMock

# elevenlabs
_stub_module("elevenlabs")

# python-dotenv
dotenv_mod = _stub_module("dotenv")
dotenv_mod.load_dotenv = MagicMock()

# pytz
pytz_mod = _stub_module("pytz")
pytz_mod.timezone = MagicMock(return_value=MagicMock())
pytz_mod.UTC = MagicMock()

# dateutil
_ensure_parent("dateutil")
dateutil_mod = _stub_module("dateutil")
dateutil_parser_mod = _stub_module("dateutil.parser")
dateutil_parser_mod.parse = MagicMock()

# loguru
loguru_mod = _stub_module("loguru")
loguru_mod.logger = MagicMock()

# aiohttp
_stub_module("aiohttp")


@pytest.fixture
def mock_supabase_client():
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table
    table.select.return_value = table
    table.insert.return_value = table
    table.update.return_value = table
    table.delete.return_value = table
    table.eq.return_value = table
    table.neq.return_value = table
    table.gte.return_value = table
    table.lte.return_value = table
    table.lt.return_value = table
    table.gt.return_value = table
    table.order.return_value = table
    table.limit.return_value = table
    table.single.return_value = table
    table.execute.return_value = MagicMock(data=[], error=None)
    return client


@pytest.fixture
def sample_lead():
    return {
        "id": "lead-123",
        "phone": "5511999999999",
        "name": "João Silva",
        "email": "joao@example.com",
        "status": "active",
        "score": 50,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "broker_id": "broker-456",
        "property_interest": "apartment",
        "budget_min": 300000,
        "budget_max": 500000,
        "last_interaction": "2024-01-01T00:00:00Z",
    }


@pytest.fixture
def sample_message_payload():
    return {
        "event": "messages.upsert",
        "instance": "test-instance",
        "data": {
            "key": {
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": False,
                "id": "msg-abc123",
            },
            "message": {
                "conversation": "Quero ver o apartamento",
            },
            "messageTimestamp": 1704067200,
            "pushName": "João Silva",
        },
    }


@pytest.fixture
def sample_audio_payload():
    return {
        "event": "messages.upsert",
        "instance": "test-instance",
        "data": {
            "key": {
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": False,
                "id": "msg-audio123",
            },
            "message": {
                "audioMessage": {
                    "url": "https://example.com/audio.ogg",
                    "mimetype": "audio/ogg; codecs=opus",
                    "seconds": 5,
                }
            },
            "messageTimestamp": 1704067200,
            "pushName": "João Silva",
        },
    }
</parameter>
</invoke>
</thinking>

<file path="tests/conftest.py">
import sys
import types
import pytest
from unittest.mock import MagicMock, AsyncMock


def _stub_module(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _ensure_parent(dotted: str):
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            sys.modules[parent] = types.ModuleType(parent)


# ---------------------------------------------------------------------------
# Stub all external / heavy dependencies BEFORE any production module loads
# ---------------------------------------------------------------------------

# supabase
_stub_module("supabase")
_stub_module("supabase.client")

# anthropic
anthropic_mod = _stub_module("anthropic")
anthropic_mod.Anthropic = MagicMock
anthropic_mod.AsyncAnthropic = MagicMock

# openai
openai_mod = _stub_module("openai")
openai_mod.OpenAI = MagicMock
openai_mod.AsyncOpenAI = MagicMock

# redis
redis_mod = _stub_module("redis")
redis_mod.Redis = MagicMock
_ensure_parent("redis.asyncio")
redis_asyncio_mod = _stub_module("redis.asyncio")
redis_asyncio_mod.Redis = MagicMock
redis_asyncio_mod.from_url = MagicMock(return_value=MagicMock())

# httpx
httpx_mod = _stub_module("httpx")
httpx_mod.AsyncClient = MagicMock
httpx_mod.Client = MagicMock
httpx_mod.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
httpx_mod.RequestError = type("RequestError", (Exception,), {})
httpx_mod.Response = MagicMock

# fastapi
fastapi_mod = _stub_module("fastapi")
fastapi_mod.FastAPI = MagicMock
fastapi_mod.APIRouter = MagicMock
fastapi_mod.Request = MagicMock
fastapi_mod.Response = MagicMock
fastapi_mod.HTTPException = type("HTTPException", (Exception,), {"__init__": lambda self, status_code=400, detail="": None})
fastapi_mod.Depends = MagicMock(return_value=None)
fastapi_mod.BackgroundTasks = MagicMock

_stub_module("fastapi.responses")
_stub_module("fastapi.middleware")
_stub_module("fastapi.middleware.cors")

# pydantic
pydantic_mod = _stub_module("pydantic")


class _BaseModel:
    def __init__(self, **data):
        for k, v in data.items():
            setattr(self, k, v)

    def dict(self):
        return self.__dict__.copy()

    def model_dump(self):
        return self.__dict__.copy()


pydantic_mod.BaseModel = _BaseModel
pydantic_mod.Field = MagicMock(return_value=None)
pydantic_mod.validator = MagicMock(return_value=lambda f: f)
pydantic_mod.field_validator = MagicMock(return_value=lambda f: f)

# celery
celery_mod = _stub_module("celery")
celery_mod.Celery = MagicMock

# elevenlabs
_stub_module("elevenlabs")

# python-dotenv
dotenv_mod = _stub_module("dotenv")
dotenv_mod.load_dotenv = MagicMock()

# pytz
import datetime as _dt

pytz_mod = _stub_module("pytz")
_utc_tz = _dt.timezone.utc


class _FakeTz(_dt.tzinfo):
    def utcoffset(self, dt):
        return _dt.timedelta(0)

    def tzname(self, dt):
        return "UTC"

    def dst(self, dt):
        return _dt.timedelta(0)


pytz_mod.UTC = _FakeTz()
pytz_mod.utc = _FakeTz()
pytz_mod.timezone = MagicMock(return_value=_FakeTz())

# dateutil
_ensure_parent("dateutil")
_stub_module("dateutil")
dateutil_parser_mod = _stub_module("dateutil.parser")
dateutil_parser_mod.parse = MagicMock()

# loguru
loguru_mod = _stub_module("loguru")
_logger = MagicMock()
loguru_mod.logger = _logger

# aiohttp
_stub_module("aiohttp")

# apscheduler
_ensure_parent("apscheduler")
_stub_module("apscheduler")
_ensure_parent("apscheduler.schedulers")
_stub_module("apscheduler.schedulers")
apscheduler_asyncio = _stub_module("apscheduler.schedulers.asyncio")
apscheduler_asyncio.AsyncIOScheduler = MagicMock


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_supabase_client():
    client = MagicMock()
    table = MagicMock()
    client.table.return_value = table
    table.select.return_value = table
    table.insert.return_value = table
    table.update.return_value = table
    table.delete.return_value = table
    table.eq.return_value = table
    table.neq.return_value = table
    table.gte.return_value = table
    table.lte.return_value = table
    table.lt.return_value = table
    table.gt.return_value = table
    table.order.return_value = table
    table.limit.return_value = table
    table.single.return_value = table
    table.execute.return_value = MagicMock(data=[], error=None)
    return client


@pytest.fixture
def sample_lead():
    return {
        "id": "lead-123",
        "phone": "5511999999999",
        "name": "João Silva",
        "email": "joao@example.com",
        "status": "active",
        "score": 50,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "broker_id": "broker-456",
        "property_interest": "apartment",
        "budget_min": 300000,
        "budget_max": 500000,
        "last_interaction": "2024-01-01T10:00:00Z",
    }


@pytest.fixture
def sample_message_payload():
    return {
        "event": "messages.upsert",
        "instance": "test-instance",
        "data": {
            "key": {
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": False,
                "id": "msg-abc123",
            },
            "message": {
                "conversation": "Quero ver o apartamento",
            },
            "messageTimestamp": 1704067200,
            "pushName": "João Silva",
        },
    }


@pytest.fixture
def sample_audio_payload():
    return {
        "event": "messages.upsert",
        "instance": "test-instance",
        "data": {
            "key": {
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": False,
                "id": "msg-audio123",
            },
            "message": {
                "audioMessage": {
                    "url": "https://example.com/audio.ogg",
                    "mimetype": "audio/ogg; codecs=opus",
                    "seconds": 5,
                }
            },
            "messageTimestamp": 1704067200,
            "pushName": "João Silva",
        },
    }