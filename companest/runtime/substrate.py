"""Use the pinned upstream CLI for authenticated control and actor ingress.

The CLI owns Kubernetes discovery, certificate rotation and router tunnelling.
This adapter owns neither actor scheduling nor snapshot/recovery policy.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

import httpx


class SubstrateError(RuntimeError):
    """A substrate operation failed; never fall back to local execution."""


@dataclass(frozen=True)
class ActorRef:
    atespace: str
    name: str

    def __post_init__(self):
        for value in (self.atespace, self.name):
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
                raise ValueError("Actor name and atespace must be DNS labels (1-63 characters)")


class SubstrateClient:
    def __init__(
        self, executable: str = "kubectl-ate", *, kubeconfig: Path | None = None,
        context: str | None = None, token_file: Path | None = None,
        timeout: float = 120,
    ):
        if timeout <= 0:
            raise ValueError("Substrate timeout must be positive")
        self.command = [str(executable)]
        for flag, value in (("--kubeconfig", kubeconfig), ("--context", context), ("--token-file", token_file)):
            if value is not None:
                self.command.extend((flag, str(value)))
        self.timeout = timeout

    @staticmethod
    async def _stop(process):
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def _command(self, *arguments: str, json_output: bool = True) -> Any:
        command = [*self.command, *arguments]
        if json_output:
            command.extend(("-o", "json"))
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SubstrateError("Cannot start kubectl-ate; install the pinned Substrate CLI") from exc
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), self.timeout)
            if process.returncode:
                # Do not expose diagnostics/credentials in public API responses.
                raise SubstrateError(f"Substrate {arguments[0]} failed (exit {process.returncode})")
            if not json_output:
                return None
            try:
                value = json.loads(stdout)
            except (ValueError, UnicodeDecodeError) as exc:
                raise SubstrateError("Substrate returned invalid JSON") from exc
            if not isinstance(value, dict):
                raise SubstrateError("Substrate returned an invalid resource")
            return value
        except asyncio.TimeoutError as exc:
            raise SubstrateError(f"Substrate {arguments[0]} timed out; outcome may be unknown") from exc
        finally:
            await self._stop(process)

    async def create_actor(self, actor: ActorRef, template: str) -> dict:
        ActorRef(actor.atespace, template)
        return await self._command(
            "create", "actor", actor.name, "-a", actor.atespace, "--template", template, "--preview",
        )

    async def get_actor(self, actor: ActorRef) -> dict:
        return await self._command("get", "actor", actor.name, "-a", actor.atespace)

    async def suspend_actor(self, actor: ActorRef) -> dict:
        return await self._command("suspend", "actor", actor.name, "-a", actor.atespace, "--mode=sync")

    async def resume_actor(self, actor: ActorRef) -> dict:
        return await self._command("resume", "actor", actor.name, "-a", actor.atespace)

    async def delete_actor(self, actor: ActorRef) -> None:
        # The pinned CLI returns a text acknowledgement even with -o json.
        await self._command("delete", "actor", actor.name, "-a", actor.atespace, json_output=False)

    @asynccontextmanager
    async def _proxy(self, actor: ActorRef, port: int):
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command, "proxy", "actor", actor.name, f"0:{port}", "-a", actor.atespace,
                "--address", "127.0.0.1",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SubstrateError("Cannot start the Substrate actor proxy") from exc
        drain = None
        try:
            async with asyncio.timeout(self.timeout):
                while line := await process.stderr.readline():
                    match = re.search(rb"Proxying 127\.0\.0\.1:(\d+) ->", line)
                    if match:
                        break
                else:
                    raise SubstrateError("Substrate actor proxy exited before opening its listener")

            async def discard_diagnostics():
                while await process.stderr.read(4096):
                    pass

            drain = asyncio.create_task(discard_diagnostics())
            yield f"http://127.0.0.1:{int(match[1])}"
        except TimeoutError as exc:
            raise SubstrateError("Substrate actor proxy timed out") from exc
        finally:
            await self._stop(process)
            if drain:
                await drain

    async def request(
        self, actor: ActorRef, method: str, path: str, *, payload: dict | None = None, port: int = 80,
    ) -> httpx.Response:
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            raise ValueError("Actor request path must be an absolute local path")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Actor port must be between 1 and 65535")
        # No retries: POST timeouts may mean that the actor accepted the task.
        try:
            async with self._proxy(actor, port) as endpoint:
                async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                    response = await client.request(method, endpoint + path, json=payload)
                    if response.is_redirect:
                        raise SubstrateError("Unexpected actor redirect")
                    return response
        except httpx.HTTPError as exc:
            raise SubstrateError("Substrate actor request failed; outcome may be unknown") from exc
