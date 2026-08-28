"""Credential Account resolution for CLI workers.

This module keeps subscription/API credentials out of prompts, worker scratch,
and the normal worker config JSON. It resolves a small, explicit account store:

    sessions/_secrets/accounts/<account_id>/

Container workers see that root at /run/dswarm/accounts. Local workers can use
the same files directly. Environment variables remain a developer convenience,
but the persistent path is account-scoped instead of mounting a host home dir.
"""

from __future__ import annotations

import os
import re
import shutil
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from dswarm.solver.secret_store import atomic_write, chmod_private_dir, updated_at


CONTAINER_ACCOUNTS_ROOT = "/run/dswarm/accounts"


@dataclass(frozen=True)
class RuntimeCredentialEnv:
    """Environment to add to a worker subprocess plus its account id."""

    account_id: str
    env: dict[str, str]


@dataclass(frozen=True)
class CredentialAccount:
    account_id: str
    engine: str
    mode: str
    present: bool
    writable_state: bool
    updated_at: float | None = None
    details: dict[str, Any] | None = None


_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def account_store_root(sessions_root: str | Path) -> Path:
    """Default durable account store under the web sessions root."""

    return Path(sessions_root) / "_secrets" / "accounts"


def engine_account_id(engine: str, env: Mapping[str, str] | None = None) -> str:
    """Return the account id for an engine, overridable per engine by env."""

    e = (engine or "").strip().lower()
    source = env or os.environ
    return (
        source.get(f"DSWARM_{e.upper()}_ACCOUNT_ID")
        or source.get("DSWARM_DEFAULT_ACCOUNT_ID")
        or f"{e}-main"
    )


def valid_account_id(account_id: str) -> bool:
    return bool(_ACCOUNT_ID_RE.fullmatch(account_id or ""))


class CredentialAccountStore:
    """Small filesystem-backed account store for subscription/API workers."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Native Windows cannot enforce POSIX owner-only bits. This protects
        # the account store on POSIX; production isolation is Docker/Linux,
        # not the development host's staging filesystem.
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def list(self) -> list[dict[str, Any]]:
        accounts: list[CredentialAccount] = []
        if not self.root.exists():
            return []
        for p in sorted(self.root.iterdir(), key=lambda x: x.name):
            if not p.is_dir() or not valid_account_id(p.name):
                continue
            acct = self.inspect(p.name)
            if acct is not None:
                accounts.append(acct)
        return [self._public(a) for a in accounts]

    def inspect(self, account_id: str) -> CredentialAccount | None:
        if not valid_account_id(account_id):
            return None
        base = self.root / account_id
        if not base.exists() or not base.is_dir():
            return None
        updated = self._updated_at(base)
        if (base / "API_KEY").exists():
            # A custom endpoint (API_KEY + BASE_URL) is engine-agnostic on disk —
            # runtime_env_for_engine keys off the ENGINE passed in, not the account.
            # The optional ENGINE marker records which engine the operator
            # registered it FOR, so the panel can bind/display it instead of an
            # orphan "api". No marker → legacy/programmatic "api".
            target = self._read_target_engine(base)
            base_url = self._read_base_url(base)
            if not base_url:
                # A provider-key account (pi's DEEPSEEK/OPENAI/ANTHROPIC key) with
                # no custom endpoint: probe/schedule it as a plain key account,
                # NOT a custom endpoint (which would demand a base_url).
                return CredentialAccount(
                    account_id=account_id,
                    engine=target or "api",
                    mode="api_key",
                    present=True,
                    writable_state=False,
                    updated_at=updated,
                    details={
                        "api_key_file": True,
                        # Kept only on the internal inspect() object for runtime
                        # consumers. Public API responses are sanitized by
                        # _public() and never expose this value.
                        "secret_value": self._read_secret_value(base),
                        "target_engine": target or None,
                    },
                )
            return CredentialAccount(
                account_id=account_id,
                engine=target or "api",
                mode="custom_endpoint",
                present=True,
                writable_state=False,
                updated_at=updated,
                details={
                    "api_key_file": True,
                    "base_url": bool(base_url),
                    # base_url is non-sensitive config. secret_value remains
                    # available only to trusted in-process runtime consumers;
                    # _public() strips it from every API response.
                    "base_url_value": base_url,
                    "secret_value": self._read_secret_value(base),
                    "custom_endpoint": True,
                    "target_engine": target or None,
                },
            )
        return CredentialAccount(
            account_id=account_id,
            engine="unknown",
            mode="empty",
            present=False,
            writable_state=False,
            updated_at=updated,
            details={},
        )

    def upsert_secret(
        self,
        *,
        account_id: str,
        engine: str,
        secret: str | None = None,
        base_url: str | None = None,
        target_engine: str | None = None,
    ) -> dict[str, Any]:
        account_id = account_id.strip()
        engine = engine.strip().lower()
        if not valid_account_id(account_id):
            raise ValueError("account_id must be 1-64 chars: letters, digits, _, ., -")
        if engine not in {"pi", "api"}:
            raise ValueError("engine must be pi or api")

        # EDIT support: secrets are never read back to the UI, so an operator who
        # only wants to change an endpoint's base_url / target_engine cannot
        # re-supply the key. When the incoming secret is blank AND a matching
        # account already exists on disk, fall back to the stored secret so the
        # edit preserves it. _replace_account wipes the dir, so snapshot first.
        prior = self._snapshot_material(account_id)

        if engine == "api":
            value = str(secret or "").strip() or prior.get("API_KEY", "")
            if not value:
                raise ValueError("API_KEY is required")
            # base_url / target_engine: a blank field on edit keeps the stored
            # value (the UI sends "" when the operator didn't touch it). An
            # explicit clear isn't expressible here, and isn't needed by the panel.
            b = str(base_url or "").strip() or prior.get("BASE_URL", "")
            te = str(target_engine or "").strip().lower() or prior.get("ENGINE", "")
            if te and te not in {"pi"}:
                raise ValueError("target_engine must be pi")
            base = self._replace_account(account_id)
            self._atomic_write(base / "API_KEY", value + "\n")
            if b:
                self._atomic_write(base / "BASE_URL", b + "\n")
            # Record which engine this endpoint is FOR so the panel can bind/display
            # it. The runtime injection stays engine-agnostic (it reads API_KEY/
            # BASE_URL regardless of this marker).
            if te:
                self._atomic_write(base / "ENGINE", te + "\n")
        else:
            # pi: the account's key file is DEEPSEEK_API_KEY (the pi CLI's
            # deepseek provider), written as API_KEY on disk and mapped to
            # DEEPSEEK_API_KEY at runtime.
            value = str(secret or "").strip() or prior.get("API_KEY", "")
            if not value:
                raise ValueError("API_KEY is required")
            base = self._replace_account(account_id)
            self._atomic_write(base / "API_KEY", value + "\n")
            # ENGINE marker: this account belongs to the pi engine (not an
            # orphan "api"), so the panel binds/displays it as pi.
            self._atomic_write(base / "ENGINE", "pi\n")

        acct = self.inspect(account_id)
        assert acct is not None
        return self._public(acct)

    def _replace_account(self, account_id: str) -> Path:
        base = self.root / account_id
        base.mkdir(parents=True, exist_ok=True)
        self._chmod_private_dir(base)
        self._clear_account_material(base)
        return base

    def delete(self, account_id: str) -> bool:
        if not valid_account_id(account_id):
            return False
        base = self.root / account_id
        if not base.exists():
            return False
        shutil.rmtree(base)
        return True

    @staticmethod
    def _public(acct: CredentialAccount) -> dict[str, Any]:
        """Return write-only-safe account metadata for HTTP/UI consumers.

        ``inspect()`` intentionally retains the raw secret for trusted runtime
        code such as the endpoint driver. The public shape is an explicit
        allow-list so a future internal detail cannot accidentally cross the API
        boundary.
        """
        details = acct.details or {}
        safe_details = {
            key: details[key]
            for key in (
                "api_key_file",
                "base_url",
                "base_url_value",
                "custom_endpoint",
                "target_engine",
            )
            if key in details
        }
        safe_details["has_secret"] = bool(acct.present and details.get("api_key_file"))
        return {
            "account_id": acct.account_id,
            "engine": acct.engine,
            "mode": acct.mode,
            "present": acct.present,
            "writable_state": acct.writable_state,
            "updated_at": acct.updated_at,
            "details": safe_details,
        }

    @staticmethod
    def _read_target_engine(base: Path) -> str:
        """The agent a custom endpoint was registered for (ENGINE marker), or ""."""
        mp = base / "ENGINE"
        if not mp.exists():
            return ""
        try:
            marker = mp.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return ""
        return marker if marker in {"pi"} else ""

    @staticmethod
    def _read_base_url(base: Path) -> str:
        """The custom endpoint's BASE_URL value, or "" if unset/unreadable.

        Non-sensitive (a public host) — safe to surface so the UI can display and
        edit it.
        """
        p = base / "BASE_URL"
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @staticmethod
    def _read_secret_value(base: Path) -> str:
        """Read the stored secret for trusted in-process runtime consumers only.

        Public account metadata is produced through ``_public()``, whose explicit
        allow-list strips ``details.secret_value``. HTTP/UI callers must use that
        public shape; the raw value remains available here only for driver/runtime
        injection paths that already execute inside the trusted backend process.
        """
        p = base / "API_KEY"
        if p.exists():
            try:
                return p.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return ""

    @staticmethod
    def _updated_at(path: Path) -> float | None:
        return updated_at(path)

    @staticmethod
    def _chmod_private_dir(path: Path) -> None:
        chmod_private_dir(path)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        atomic_write(path, text)

    def _snapshot_material(self, account_id: str) -> dict[str, str]:
        """Read an existing account's stored secrets/markers before a rewrite.

        Returns a dict keyed by the on-disk filename holding the trimmed prior
        values, or empty strings for anything absent. Used so a metadata-only
        edit (blank secret) can fall back to the stored credential instead of
        erroring or wiping it. Never raises — a fresh/unreadable account simply
        yields blanks.
        """
        base = self.root / account_id
        out: dict[str, str] = {}
        for rel in ("API_KEY", "BASE_URL", "ENGINE"):
            p = base / rel
            try:
                out[rel] = p.read_text(encoding="utf-8").strip() if p.exists() else ""
            except OSError:
                out[rel] = ""
        return out

    @staticmethod
    def _clear_account_material(base: Path) -> None:
        for rel in ("API_KEY", "BASE_URL", "ENGINE"):
            try:
                (base / rel).unlink(missing_ok=True)
            except OSError:
                pass


def runtime_env_for_engine(
    engine: str,
    *,
    account_root: str | Path | None = None,
    account_id: str | None = None,
    container: bool = False,
    env: Mapping[str, str] | None = None,
) -> RuntimeCredentialEnv:
    """Resolve credential env for one engine.

    Container mode avoids sending secret values through `docker exec -e` when a
    file-backed account exists: it passes only `*_FILE` paths and lets the
    container shell export the real value inside the process. Local mode reads
    those files into the subprocess env because there is no container wrapper.
    """

    e = (engine or "").strip().lower()
    source = env or os.environ
    if account_id is None:
        account_id = engine_account_id(e, source)
    elif account_id != "" and not valid_account_id(account_id):
        account_id = engine_account_id(e, source)
    root = Path(account_root).expanduser().resolve() if account_root is not None else None
    base = root / account_id if root is not None and account_id else None
    out: dict[str, str] = {}

    # pi: the pi CLI reads standard provider env keys (ANTHROPIC_API_KEY /
    # OPENAI_API_KEY / DEEPSEEK_API_KEY) — which provider it uses is decided by
    # DSWARM_PI_PROVIDER / --provider on the driver. A custom-endpoint account
    # maps to the OpenAI-compatible path so pi's openai provider can consume it.
    if e == "pi":
        if base is not None and (base / "API_KEY").exists():
            prov = str(source.get("DSWARM_PI_PROVIDER", "")).strip().lower()
            if prov == "deepseek":
                # pi's deepseek provider (models-store.json) reads DEEPSEEK_API_KEY;
                # the base URL is baked into the provider definition, not env.
                _add_secret_file_or_env(
                    out,
                    base=base,
                    filename="API_KEY",
                    env_name="DEEPSEEK_API_KEY",
                    container=container,
                    container_path=_container_secret_path(account_id, "API_KEY"),
                    source=source,
                )
            elif prov in ("openai", "custom", "dswarm-worker"):
                _add_secret_file_or_env(
                    out,
                    base=base,
                    filename="API_KEY",
                    env_name="OPENAI_API_KEY",
                    container=container,
                    container_path=_container_secret_path(account_id, "API_KEY"),
                    source=source,
                )
                _add_base_url(out, base=base, env_name="OPENAI_BASE_URL")
            else:
                _add_secret_file_or_env(
                    out,
                    base=base,
                    filename="API_KEY",
                    env_name="ANTHROPIC_API_KEY",
                    container=container,
                    container_path=_container_secret_path(account_id, "API_KEY"),
                    source=source,
                )
                _add_base_url(out, base=base, env_name="ANTHROPIC_BASE_URL")

    return RuntimeCredentialEnv(account_id=account_id, env=out)


def _container_secret_path(account_id: str, filename: str) -> str:
    return f"{CONTAINER_ACCOUNTS_ROOT}/{account_id}/{filename}"


def _add_secret_file_or_env(
    out: dict[str, str],
    *,
    base: Optional[Path],
    filename: str,
    env_name: str,
    container: bool,
    container_path: str,
    source: Mapping[str, str],
) -> None:
    if base is not None:
        p = base / filename
        if p.exists():
            if container:
                out[f"{env_name}_FILE"] = container_path
            else:
                try:
                    value = p.read_text(encoding="utf-8").strip()
                except OSError:
                    value = ""
                if value:
                    out[env_name] = value
            return
    if source.get(env_name):
        out[env_name] = str(source[env_name])


def _add_base_url(out: dict[str, str], *, base: Optional[Path], env_name: str) -> None:
    if base is None:
        return
    p = base / "BASE_URL"
    if not p.exists():
        return
    try:
        value = p.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        out[env_name] = value


def detect_system_login(engine: str, env: Mapping[str, str] | None = None) -> str:
    """Is there a usable HOST-side login for this engine? (DESIGN §2.3 補強B)

    READ-ONLY, never raises. Returns "present" / "absent" / "unknown". This only
    drives the local-mode credentials UI: in local mode a worker inherits the
    host HOME+env, so an unregistered account silently falls back to the host's
    existing CLI login. Container mode does NOT use this (host login isn't
    mounted) — there an account is mandatory.
    """
    e = (engine or "").strip().lower()
    source = env or os.environ

    if e == "pi":
        # the pi CLI's deepseek provider reads DEEPSEEK_API_KEY; a present host
        # key means a usable host-side login.
        if source.get("DEEPSEEK_API_KEY"):
            return "present"
        return "absent"

    return "unknown"


def ensure_pi_account_from_env(
    sessions_root: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Auto-register default pi accounts from the host provider key when safe.

    Container workers need a local account file so the key can be mounted into
    the worker image. For a pi-only deployment this should be invisible to the
    operator: if DEEPSEEK_API_KEY / DSWARM_DEEPSEEK_API_KEY is present, mirror it
    into each default direction account at startup. Never overwrite an existing
    account that the operator has customized.
    """
    source = env or os.environ
    flag = str(source.get("DSWARM_AUTO_BIND_PI_ACCOUNT", "1")).strip().lower()
    if flag in {"0", "false", "no", "off", ""}:
        return False
    key = str(
        source.get("DSWARM_DEEPSEEK_API_KEY")
        or source.get("DEEPSEEK_API_KEY")
        or ""
    ).strip()
    if not key:
        return False
    root = account_store_root(sessions_root)
    store = CredentialAccountStore(root)
    account_ids = [
        "pi-main",
        "pi-web-main",
        "pi-pwn-main",
        "pi-rev-main",
        "pi-crypto-main",
        "pi-misc-main",
        "pi-forensics-main",
        "pi-aisec-main",
    ]
    created = False
    for account_id in account_ids:
        existing = store.inspect(account_id)
        if existing is not None and existing.mode == "custom_endpoint":
            continue
        if existing is not None and existing.present:
            continue
        store.upsert_secret(account_id=account_id, engine="pi", secret=key)
        created = True
    return created


# Filenames whose containing dir must be WRITABLE inside the container so the CLI
# can refresh state in place. pi's key file is read-only for the worker.
_WRITABLE_STATE_DIRS = ()


def project_account_root(src_root: str | Path, dest_root: str | Path) -> Path:
    """Stage a container-READABLE projection of the account store (#14, #15).

    The host account store holds 0600 files owned by the host user; a container
    worker runs as a different uid ('kali') and cannot read them through a plain
    read-only bind mount (#15).

    This copies the store into `dest_root` (a per-run, gitignored, ephemeral dir
    under the run workspace) with permissions the container user can use:
      - static secret files (API keys) → 0644 (readable, not writable by the
        worker — the worker only reads them).
    The HOST store is never modified and never made world-writable; this projection
    is the only thing the container sees. Returns dest_root.
    """
    warnings.warn(
        "legacy broad credential projection; use CredentialProjector",
        DeprecationWarning,
        stacklevel=2,
    )
    src = Path(src_root)
    dest = Path(dest_root)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    # This projection is intentionally container-readable. On native Windows,
    # chmod is best-effort and host staging is not a security boundary.
    try:
        os.chmod(dest, 0o755)
    except OSError:
        pass
    if not src.exists():
        return dest
    for account_dir in sorted(p for p in src.iterdir() if p.is_dir()):
        if not valid_account_id(account_dir.name):
            continue
        out_account = dest / account_dir.name
        out_account.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(out_account, 0o755)
        except OSError:
            pass
        for item in account_dir.iterdir():
            target = out_account / item.name
            if item.is_dir():
                writable = item.name in _WRITABLE_STATE_DIRS
                shutil.copytree(item, target, dirs_exist_ok=True)
                _chmod_tree(target, dir_mode=0o777 if writable else 0o755,
                            file_mode=0o666 if writable else 0o644)
            elif item.is_file():
                shutil.copy2(item, target)
                try:
                    os.chmod(target, 0o644)
                except OSError:
                    pass
    return dest


def _chmod_tree(root: Path, *, dir_mode: int, file_mode: int) -> None:
    # Permission modes remain functional metadata for the Linux container;
    # native Windows hosts cannot provide equivalent owner/group isolation.
    for p in root.rglob("*"):
        try:
            os.chmod(p, dir_mode if p.is_dir() else file_mode)
        except OSError:
            pass
    try:
        os.chmod(root, dir_mode)
    except OSError:
        pass
