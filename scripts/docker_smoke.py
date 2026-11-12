import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "checkstock-precommit"


def run(command, *, env, check=True):
    print(f"\n$ {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=ROOT, env=env, check=check)


def main():
    env = os.environ.copy()
    env.setdefault("SESSION_SECRET", "precommit-session-secret-not-for-production")
    env.setdefault("ADMIN_USERNAME", "precommit-admin")
    env.setdefault("ADMIN_PASSWORD", "precommit-admin-password-123")
    env.setdefault("COOKIE_SECURE", "false")

    env["POSTGRES_HOST_PORT"] = "55433"
    env["WEB_PORT"] = "18005"
    env["BASE_URL"] = "http://127.0.0.1:18005"

    compose = ["docker", "compose", "-p", PROJECT_NAME]

    try:
        run(
            compose + ["up", "--build", "-d", "--wait", "--wait-timeout", "120"],
            env=env,
        )
        run([sys.executable, "scripts/smoke.py"], env=env)
        print("\nDocker Compose smoke test passed.", flush=True)
        return 0
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"\nDocker Compose smoke test failed: {exc}", file=sys.stderr)
        try:
            run(compose + ["logs", "--no-color"], env=env, check=False)
        except OSError:
            pass
        return 1
    finally:
        try:
            run(
                compose + ["down", "--volumes", "--remove-orphans"],
                env=env,
                check=False,
            )
        except OSError as exc:
            print(f"Could not clean up smoke-test stack: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
