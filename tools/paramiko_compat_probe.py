"""Branch-only Paramiko compatibility experiment; no production endpoints."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def client(port, mode, version):
    import paramiko

    from aruba2930f_backup.collector import ArubaCollector
    from aruba2930f_backup.hostkeys import HostKeyStore, sha256_fingerprint
    from aruba2930f_backup.models import (
        CollectionOptions, Credentials, DeviceStatus, DeviceTarget, ErrorCode,
        HostKeyCheck, HostKeyObservation, HostKeyTrustState,
    )
    from tests.loopback_server import LoopbackArubaSSHServer

    assert paramiko.__version__ == version, paramiko.__version__
    target = DeviceTarget("127.0.0.1", int(port))
    options = CollectionOptions(
        concurrency=1, max_attempts=1, connect_timeout_seconds=3,
        command_timeout_seconds=5,
    )
    with tempfile.TemporaryDirectory() as directory:
        store = HostKeyStore(Path(directory) / "known_hosts.json")
        collector = ArubaCollector(host_key_store=store)
        credentials = Credentials(
            LoopbackArubaSSHServer.username, LoopbackArubaSSHServer.password,
        )
        if mode == "changed":
            store.approve([HostKeyCheck(
                observation=HostKeyObservation(
                    target, "ssh-rsa", sha256_fingerprint(b"different-loopback-key"),
                ),
                state=HostKeyTrustState.UNKNOWN,
            )])
            result = collector.collect_one(target, credentials, options=options)
            assert result.status is DeviceStatus.FAILED
            assert result.error_code is ErrorCode.HOST_KEY_CHANGED, result.error_code
            print("CHANGED_KEY_BLOCKED", flush=True)
            return 0

        check = collector.probe_host_keys([target], options=options)[0]
        print(f"PROBE state={check.state} error={check.error_code}", flush=True)
        if check.state is HostKeyTrustState.REJECTED:
            assert check.error_code is ErrorCode.SSH_ALGORITHM_INCOMPATIBLE, check.error_code
            assert not check.retryable
            return 2
        assert check.state is HostKeyTrustState.UNKNOWN, check.state
        assert check.observation.fingerprint.startswith("SHA256:")
        if mode == "probe":
            return 0
        collector.approve_host_keys([check])
        result = collector.collect_one(target, credentials, options=options)
        assert result.status is DeviceStatus.SUCCESS, result.error_code
        assert result.config_sha256
        print("APPROVED_AUTH_AND_READ_ONLY_COLLECTION_OK", flush=True)
        return 0


def run(candidate, version):
    import paramiko

    from tests.loopback_server import LoopbackArubaSSHServer, wait_for

    assert paramiko.__version__ == "4.0.0", paramiko.__version__
    print(f"Server Paramiko={paramiko.__version__}; client={version}", flush=True)
    incompatible = False
    for core in ("src", "local-web/core"):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / core), str(ROOT)]))
        for legacy in (False, True):
            with LoopbackArubaSSHServer(legacy_algorithms_only=legacy) as server:
                def invoke(mode):
                    print(f"{core} legacy={legacy} mode={mode}", flush=True)
                    return subprocess.run(
                        [candidate, str(Path(__file__).resolve()), "--client",
                         str(server.port), mode, version],
                        cwd=ROOT, env=env, timeout=90, check=False,
                    ).returncode

                code = invoke("probe")
                assert not server.auth_attempts and not server.commands
                if code == 2:
                    assert legacy, "Modern peer unexpectedly incompatible"
                    print("LEGACY_KEX_INCOMPATIBLE_NO_AUTH_NO_COMMANDS", flush=True)
                    incompatible = True
                    continue
                assert code == 0, code
                assert invoke("changed") == 0
                assert not server.auth_attempts and not server.commands
                assert invoke("collect") == 0
                assert len(server.auth_attempts) == 1
                assert wait_for(lambda: "show running-config" in server.commands)
                assert server.commands.count("show running-config") == 1
                assert all(c in {
                    "no page", "terminal width 511", "show version",
                    "show modules", "show running-config", "logout",
                } for c in server.commands)
    if incompatible:
        print("COMPATIBILITY GATE FAILED: legacy fixture cannot negotiate", flush=True)
        return 1
    print("COMPATIBILITY GATE PASSED: both cores, both peer profiles", flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "--client":
        sys.exit(client(*sys.argv[2:]))
    sys.exit(run(*sys.argv[1:]))
