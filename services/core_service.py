"""Shared infrastructure: settings, security (passwords, JWT, encryption), Cosmos DB repository, Blob Storage and async helpers."""

from __future__ import annotations
import asyncio
import base64
import bcrypt
import hashlib
import json
import jwt
import sys
import threading
import uuid
from abc import ABC, abstractmethod
from cryptography.fernet import Fernet, InvalidToken
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote
from config import get_settings


MIN_PASSWORD_LENGTH = 8


_TOKEN_TYPE = "access"
_REFRESH_TOKEN_TYPE = "refresh"


class AuthError(Exception):
    """Invalid, expired or malformed credentials / token."""


class ConfigError(RuntimeError):
    """A required secret is missing from the environment."""


def _prehash(password: str) -> bytes:
    # bcrypt only looks at the first 72 bytes and rejects longer input; hashing first keeps long passphrases intact.
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except ValueError:
        return False


# A real hash to compare against when the email is unknown, so login takes the same time either way.
DUMMY_HASH = hash_password("not-a-real-password")


def _jwt_secret() -> str:
    secret = get_settings().jwt_secret
    if len(secret) < 32:
        raise ConfigError("JWT_SECRET must be set to a random string of at least 32 characters.")
    return secret


def create_access_token(user_id: str, email: str) -> Dict[str, Any]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": user_id, "email": email, "type": _TOKEN_TYPE, "iat": now, "exp": expires}
    token = jwt.encode(payload, _jwt_secret(), algorithm=settings.jwt_algorithm)
    return {"access_token": token, "token_type": "bearer", "expires_at": expires.isoformat()}


def decode_access_token(token: str) -> Dict[str, Any]:
    """Returns the claims of a valid access token, raises AuthError otherwise."""
    settings = get_settings()
    try:
        claims = jwt.decode(token, _jwt_secret(), algorithms=[settings.jwt_algorithm], options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError as e:
        raise AuthError("Token has expired") from e
    except jwt.PyJWTError as e:
        raise AuthError("Invalid token") from e
    if claims.get("type") != _TOKEN_TYPE or not claims.get("email"):
        raise AuthError("Invalid token")
    return claims


def create_refresh_token(user_id: str, email: str) -> str:
    """Generates a long-lived JWT refresh token."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=getattr(settings, "jwt_refresh_expire_days", 30))
    payload = {"sub": user_id, "email": email, "type": _REFRESH_TOKEN_TYPE, "iat": now, "exp": expires}
    return jwt.encode(payload, _jwt_secret(), algorithm=settings.jwt_algorithm)


def decode_refresh_token(token: str) -> Dict[str, Any]:
    """Returns the claims of a valid refresh token, raises AuthError otherwise."""
    settings = get_settings()
    try:
        claims = jwt.decode(token, _jwt_secret(), algorithms=[settings.jwt_algorithm], options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError as e:
        raise AuthError("Refresh token has expired") from e
    except jwt.PyJWTError as e:
        raise AuthError("Invalid refresh token") from e
    if claims.get("type") != _REFRESH_TOKEN_TYPE or not claims.get("email"):
        raise AuthError("Invalid refresh token")
    return claims


def generate_encryption_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _fernet() -> Fernet:
    key = get_settings().credential_key
    if not key:
        raise ConfigError("CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with: python -c \"from services.core_service import generate_encryption_key as g; print(g())\"")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as e:
        raise ConfigError("CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key.") from e


def encrypt_text(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_text(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ConfigError("Stored secret could not be decrypted - CREDENTIAL_ENCRYPTION_KEY has changed.") from e


def encrypt_json(value: Any) -> str:
    return encrypt_text(json.dumps(value))


def decrypt_json(token: str) -> Any:
    return json.loads(decrypt_text(token))


Doc = Dict[str, Any]


def new_id() -> str:
    """Random uuid4 as a lowercase string (valid as a blob container name too)."""
    return str(uuid.uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EmailAlreadyExists(Exception):
    pass


def platform_root(platform: str) -> str:
    """'Indeed - Company Site' -> 'indeed'."""
    return (platform or "").split(" - ")[0].strip().lower()


class Repository(ABC):
    # ---- users
    @abstractmethod
    def create_user(self, doc: Doc) -> Doc: ...
    @abstractmethod
    def get_user(self, user_id: str, email: str) -> Optional[Doc]: ...
    @abstractmethod
    def get_user_by_email(self, email: str) -> Optional[Doc]: ...

    # ---- profile
    @abstractmethod
    def get_profile(self, user_id: str) -> Optional[Doc]: ...
    @abstractmethod
    def save_profile(self, user_id: str, doc: Doc) -> Doc: ...

    # ---- resumes
    @abstractmethod
    def save_resume(self, doc: Doc) -> Doc: ...
    @abstractmethod
    def get_resume(self, user_id: str, resume_id: str) -> Optional[Doc]: ...
    @abstractmethod
    def list_resumes(self, user_id: str) -> List[Doc]: ...
    @abstractmethod
    def delete_resume(self, user_id: str, resume_id: str) -> None: ...

    # ---- platform credentials (id == platform name)
    @abstractmethod
    def save_credentials(self, doc: Doc) -> Doc: ...
    @abstractmethod
    def get_credentials(self, user_id: str, platform: str) -> Optional[Doc]: ...
    @abstractmethod
    def list_credentials(self, user_id: str) -> List[Doc]: ...
    @abstractmethod
    def delete_credentials(self, user_id: str, platform: str) -> None: ...

    # ---- applications
    @abstractmethod
    def save_application(self, doc: Doc) -> Doc: ...
    @abstractmethod
    def get_application(self, user_id: str, application_id: str) -> Optional[Doc]: ...
    @abstractmethod
    def find_recent_application(self, user_id: str, job_key: str, since_iso: str) -> Optional[Doc]: ...
    @abstractmethod
    def list_applications(self, user_id: str, run_id: Optional[str] = None, limit: int = 500) -> List[Doc]: ...

    # ---- runs
    @abstractmethod
    def save_run(self, doc: Doc) -> Doc: ...
    @abstractmethod
    def get_run(self, user_id: str, run_id: str) -> Optional[Doc]: ...
    @abstractmethod
    def list_runs(self, user_id: str, limit: int = 20) -> List[Doc]: ...


_CONTAINERS = {
    "users": "/email",
    "profiles": "/user_id",
    "resumes": "/user_id",
    "credentials": "/user_id",
    "applications": "/user_id",
    "runs": "/user_id",
}


def _clean(doc: Optional[Doc]) -> Optional[Doc]:
    """Drops Cosmos system properties (_rid, _etag, _ts ...)."""
    if doc is None:
        return None
    return {k: v for k, v in doc.items() if not k.startswith("_")}


class CosmosRepository(Repository):
    def __init__(self, endpoint: str, key: str, database: str):
        from azure.cosmos import CosmosClient

        if not endpoint or not key:
            raise RuntimeError("COSMOS_ENDPOINT and COSMOS_KEY must be set.")
        self._client = CosmosClient(endpoint, credential=key)
        self._database_name = database
        self._containers: Dict[str, Any] = {}

    def ensure_containers(self) -> None:
        """Creates the database and containers if they do not exist yet."""
        from azure.cosmos import PartitionKey

        database = self._client.create_database_if_not_exists(id=self._database_name)
        for name, pk_path in _CONTAINERS.items():
            kwargs: Dict[str, Any] = {}
            if name == "users":
                kwargs["unique_key_policy"] = {"uniqueKeys": [{"paths": ["/email"]}]}
            self._containers[name] = database.create_container_if_not_exists(
                id=name, partition_key=PartitionKey(path=pk_path), **kwargs
            )

    def _c(self, name: str):
        if name not in self._containers:
            self.ensure_containers()
        return self._containers[name]

    def _read(self, container: str, item_id: str, partition_key: str) -> Optional[Doc]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return _clean(self._c(container).read_item(item=item_id, partition_key=partition_key))
        except CosmosResourceNotFoundError:
            return None

    def _query(self, container: str, query: str, params: Dict[str, Any], partition_key: str) -> List[Doc]:
        parameters = [{"name": f"@{k}", "value": v} for k, v in params.items()]
        items = self._c(container).query_items(query=query, parameters=parameters, partition_key=partition_key)
        return [_clean(i) for i in items]

    # ---- users
    def create_user(self, doc: Doc) -> Doc:
        from azure.cosmos.exceptions import CosmosResourceExistsError

        try:
            return _clean(self._c("users").create_item(body=doc))
        except CosmosResourceExistsError as e:
            raise EmailAlreadyExists(doc.get("email")) from e

    def get_user(self, user_id: str, email: str) -> Optional[Doc]:
        return self._read("users", user_id, email)

    def get_user_by_email(self, email: str) -> Optional[Doc]:
        rows = self._query("users", "SELECT * FROM c WHERE c.email = @email", {"email": email}, partition_key=email)
        return rows[0] if rows else None

    # ---- profile
    def get_profile(self, user_id: str) -> Optional[Doc]:
        return self._read("profiles", user_id, user_id)

    def save_profile(self, user_id: str, doc: Doc) -> Doc:
        doc = {**doc, "id": user_id, "user_id": user_id}
        return _clean(self._c("profiles").upsert_item(body=doc))

    # ---- resumes
    def save_resume(self, doc: Doc) -> Doc:
        return _clean(self._c("resumes").upsert_item(body=doc))

    def get_resume(self, user_id: str, resume_id: str) -> Optional[Doc]:
        return self._read("resumes", resume_id, user_id)

    def list_resumes(self, user_id: str) -> List[Doc]:
        return self._query("resumes", "SELECT * FROM c WHERE c.user_id = @u ORDER BY c.uploaded_at DESC", {"u": user_id}, user_id)

    def delete_resume(self, user_id: str, resume_id: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            self._c("resumes").delete_item(item=resume_id, partition_key=user_id)
        except CosmosResourceNotFoundError:
            pass

    # ---- credentials
    def save_credentials(self, doc: Doc) -> Doc:
        return _clean(self._c("credentials").upsert_item(body=doc))

    def get_credentials(self, user_id: str, platform: str) -> Optional[Doc]:
        return self._read("credentials", platform, user_id)

    def list_credentials(self, user_id: str) -> List[Doc]:
        return self._query("credentials", "SELECT * FROM c WHERE c.user_id = @u", {"u": user_id}, user_id)

    def delete_credentials(self, user_id: str, platform: str) -> None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            self._c("credentials").delete_item(item=platform, partition_key=user_id)
        except CosmosResourceNotFoundError:
            pass

    # ---- applications
    def save_application(self, doc: Doc) -> Doc:
        return _clean(self._c("applications").upsert_item(body=doc))

    def get_application(self, user_id: str, application_id: str) -> Optional[Doc]:
        return self._read("applications", application_id, user_id)

    def find_recent_application(self, user_id: str, job_key: str, since_iso: str) -> Optional[Doc]:
        rows = self._query(
            "applications",
            "SELECT * FROM c WHERE c.user_id = @u AND c.job_key = @k AND c.updated_at >= @s ORDER BY c.updated_at DESC",
            {"u": user_id, "k": job_key, "s": since_iso},
            user_id,
        )
        return rows[0] if rows else None

    def list_applications(self, user_id: str, run_id: Optional[str] = None, limit: int = 500) -> List[Doc]:
        query = "SELECT * FROM c WHERE c.user_id = @u"
        params: Dict[str, Any] = {"u": user_id}
        if run_id:
            query += " AND c.run_id = @r"
            params["r"] = run_id
        query += " ORDER BY c.updated_at DESC OFFSET 0 LIMIT @n"
        params["n"] = limit
        return self._query("applications", query, params, user_id)

    # ---- runs
    def save_run(self, doc: Doc) -> Doc:
        return _clean(self._c("runs").upsert_item(body=doc))

    def get_run(self, user_id: str, run_id: str) -> Optional[Doc]:
        return self._read("runs", run_id, user_id)

    def list_runs(self, user_id: str, limit: int = 20) -> List[Doc]:
        return self._query("runs", "SELECT * FROM c WHERE c.user_id = @u ORDER BY c.created_at DESC OFFSET 0 LIMIT @n",
                           {"u": user_id, "n": limit}, user_id)


MAX_SAS_DAYS = 365


@dataclass(frozen=True)
class SignedLinks:
    view_url: str       # opens inline in the browser (PDF preview)
    download_url: str   # forces a file download
    expires_at: str     # ISO timestamp


def _disposition(kind: str, filename: str) -> str:
    safe = "".join(ch for ch in filename if ch.isprintable() and ch not in '"\\/;').strip() or "resume"
    ascii_name = safe.encode("ascii", "ignore").decode("ascii") or "resume"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe)}"


class BlobStore:
    def __init__(self, connection_string: str):
        from azure.storage.blob import BlobServiceClient

        if not connection_string:
            raise RuntimeError("AZURE_STORAGE_CONNECTION_STRING must be set.")
        self._svc = BlobServiceClient.from_connection_string(connection_string)
        credential = self._svc.credential
        self._account_name: str = self._svc.account_name
        self._account_key: Optional[str] = getattr(credential, "account_key", None)
        if not self._account_key:
            raise RuntimeError("The storage connection string must include an AccountKey (needed to sign SAS links).")
        self._ensured: set[str] = set()

    # ---- files
    def ensure_container(self, user_id: str) -> None:
        from azure.core.exceptions import ResourceExistsError

        if user_id in self._ensured:
            return
        try:
            self._svc.create_container(user_id)
        except ResourceExistsError:
            pass
        self._ensured.add(user_id)

    def upload(self, user_id: str, blob_name: str, data: bytes, content_type: str, metadata: Optional[Dict[str, str]] = None) -> None:
        from azure.storage.blob import ContentSettings

        self.ensure_container(user_id)
        self._svc.get_blob_client(user_id, blob_name).upload_blob(
            data, overwrite=True, content_settings=ContentSettings(content_type=content_type), metadata=metadata or {}
        )

    def download(self, user_id: str, blob_name: str) -> bytes:
        return self._svc.get_blob_client(user_id, blob_name).download_blob().readall()

    def delete(self, user_id: str, blob_name: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self._svc.get_blob_client(user_id, blob_name).delete_blob()
        except ResourceNotFoundError:
            pass

    # ---- links
    def signed_links(self, user_id: str, blob_name: str, filename: str, content_type: str, days: int = MAX_SAS_DAYS) -> SignedLinks:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        days = max(1, min(int(days), MAX_SAS_DAYS))
        now = datetime.now(timezone.utc)
        expiry = now + timedelta(days=days)
        base_url = self._svc.get_blob_client(user_id, blob_name).url

        def link(kind: str) -> str:
            token = generate_blob_sas(
                account_name=self._account_name,
                container_name=user_id,
                blob_name=blob_name,
                account_key=self._account_key,
                permission=BlobSasPermissions(read=True),
                start=now - timedelta(minutes=5),  # tolerate clock skew
                expiry=expiry,
                content_disposition=_disposition(kind, filename),
                content_type=content_type,
            )
            return f"{base_url}?{token}"

        return SignedLinks(view_url=link("inline"), download_url=link("attachment"), expires_at=expiry.isoformat())


def new_worker_loop() -> asyncio.AbstractEventLoop:
    """A fresh event loop that can spawn browser subprocesses (Proactor on Windows)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


def run_in_worker_thread(fn: Callable[..., Awaitable[Any]], *args: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
    """
    Runs `await fn(*args, **kwargs)` in a dedicated thread with its own event loop and returns the result.
    Raises whatever the coroutine raised, or TimeoutError if it takes longer than `timeout` seconds.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        loop = new_worker_loop()
        try:
            box["value"] = loop.run_until_complete(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = e
        finally:
            try:
                loop.close()
            except Exception:
                pass

    thread = threading.Thread(target=target, daemon=True, name=f"worker-{getattr(fn, '__name__', 'task')}")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', 'task')} did not finish within {timeout} seconds")
    if "error" in box:
        raise box["error"]
    return box.get("value")
