# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Record exact source, setup, import origin, and unmodified full tox results."""

from datetime import datetime
from datetime import timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BASE = os.environ["SOURCE_BASE"]
CONFIG = {
    "tox.ini": (
        "7eec9b6b133fc752f30a444e42d67689a38a1df08bfb8cb9ce49f28e30b6bfbf"
    ),
    "pyproject.toml": os.environ["PROJECT_CONFIG_SHA256"],
}
phase = sys.argv[1]
assert phase in ("proof", "setup", "tests", "after")
cwd = Path.cwd().resolve()
evidence = cwd.parent / "validation-evidence"
evidence.mkdir(exist_ok=True)
record = {
    "phase": phase,
    "started_at": datetime.now(timezone.utc).isoformat(),
    "steps": [],
    "tox_command_completed": False,
    "resolution_cutoff": os.environ["UV_EXCLUDE_NEWER"],
    "lock_kind": (
        "runner-generated untracked lock; not equal to prior local resolution"
        " by assertion"
    ),
}


def sha(content):
  return hashlib.sha256(content).hexdigest()


def git(*args):
  return subprocess.check_output(["git", *args], cwd=cwd)


def source_proof():
  assert git("rev-parse", "HEAD").decode().strip() == os.environ["SOURCE_HEAD"]
  assert git("rev-parse", "HEAD^").decode().strip() == BASE
  assert (
      git("rev-parse", "HEAD^{tree}").decode().strip()
      == os.environ["SOURCE_TREE"]
  )
  assert (
      sha(git("diff", "--full-index", "--binary", BASE, "HEAD"))
      == os.environ["SOURCE_PATCH"]
  )
  assert not git("diff", "HEAD", "--name-only").strip()
  for name, expected in CONFIG.items():
    content = (cwd / name).read_bytes()
    assert content == git("show", BASE + ":" + name)
    assert sha(content) == expected
  assert (
      sha((cwd / os.environ["SOURCE_FILE"]).read_bytes())
      == os.environ["SOURCE_SHA256"]
  )
  return {
      "head": os.environ["SOURCE_HEAD"],
      "base": BASE,
      "tree": os.environ["SOURCE_TREE"],
      "patch_sha256": os.environ["SOURCE_PATCH"],
      "source_sha256": os.environ["SOURCE_SHA256"],
      "configuration_sha256": CONFIG,
      "unchanged_source_and_configuration": True,
  }


def run(name, args):
  started = time.monotonic()
  path = evidence / (name + ".log")
  with path.open("w", encoding="utf-8") as log:
    log.write("Command: " + json.dumps(args) + "\n")
    log.flush()
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    for line in proc.stdout:
      print(line, end="", flush=True)
      log.write(line)
    code = proc.wait()
  record["steps"].append({
      "name": name,
      "args": args,
      "exit_code": code,
      "log": path.name,
      "elapsed_seconds": round(time.monotonic() - started, 2),
  })
  if code:
    raise subprocess.CalledProcessError(code, args)


try:
  record["source_before"] = source_proof()
  if phase == "setup":
    uv = shutil.which("uv")
    assert uv and subprocess.check_output([uv, "--version"]).decode().split()[
        :2
    ] == ["uv", "0.12.23"]
    os.environ["TOX_UV_PATH"] = uv
    record["TOX_UV_PATH"] = uv
    run(
        "uv-venv",
        [
            uv,
            "venv",
            "--python",
            "python" + os.environ["PYTHON_VERSION"],
            ".venv",
        ],
    )
    run("uv-sync-all-extras", [uv, "sync", "--all-extras"])
    record["generated_lock_sha256"] = sha((cwd / "uv.lock").read_bytes())
    (evidence / "generated-uv.lock").write_bytes((cwd / "uv.lock").read_bytes())
    run(
        "tox-tool-install",
        [uv, "tool", "install", "tox==4.65.3", "--with", "tox-uv==1.37.0"],
    )
    tox = str(
        Path(
            subprocess.check_output([uv, "tool", "dir", "--bin"])
            .decode()
            .strip()
        )
        / "tox"
    )
    tool_python = str(Path(tox).resolve().parent / "python")
    versions = (
        "import importlib.metadata,json;"
        "v={n:importlib.metadata.version(n) for n in ('tox','tox-uv')};"
        "print(json.dumps(v));assert v=={'tox':'4.65.3','tox-uv':'1.37.0'}"
    )
    run("tox-tool-versions", [tool_python, "-c", versions])
    run(
        "tox-pristine-lane-setup",
        [tox, "--notest", "-e", os.environ["TOX_LANE"]],
    )
    probe = (
        "import importlib,hashlib,json,os,sys;from pathlib import Path;"
        "m=importlib.import_module(os.environ['SOURCE_MODULE']);"
        "actual=Path(m.__file__).resolve();expected=Path(os.environ['SOURCE_FILE']).resolve();"
        "digest=hashlib.sha256(actual.read_bytes()).hexdigest();"
        "print(json.dumps({'python':sys.version,'executable':sys.executable,'origin':str(actual),'expected':str(expected),'source_sha256':digest}));"
        "assert actual==expected;assert digest==os.environ['SOURCE_SHA256'];"
        "assert"
        " sys.version_info[:2]==tuple(map(int,os.environ['PYTHON_VERSION'].split('.')))"
    )
    run(
        "lane-import-origin",
        [
            str(cwd / ".tox" / os.environ["TOX_LANE"] / "bin/python"),
            "-c",
            probe,
        ],
    )
  elif phase == "tests":
    uv = shutil.which("uv")
    assert uv and subprocess.check_output([uv, "--version"]).decode().split()[
        :2
    ] == ["uv", "0.12.23"]
    os.environ["TOX_UV_PATH"] = uv
    record["TOX_UV_PATH"] = uv
    assert (evidence / "lane-import-origin.log").exists()
    tox = str(
        Path(
            subprocess.check_output([uv, "tool", "dir", "--bin"])
            .decode()
            .strip()
        )
        / "tox"
    )
    try:
      run("full-tox-unit-lane", [tox, "-e", os.environ["TOX_LANE"]])
    finally:
      if any(step["name"] == "full-tox-unit-lane" for step in record["steps"]):
        record["tox_command_completed"] = True
  elif phase == "after" and (evidence / "generated-uv.lock").exists():
    lock = (cwd / "uv.lock").read_bytes()
    assert lock == (evidence / "generated-uv.lock").read_bytes()
    record["generated_lock_sha256"] = sha(lock)
    record["generated_lock_unchanged"] = True
  record["source_after"] = source_proof()
  record["state"] = "passed"
except BaseException as exc:
  record.update(state="failed", error=repr(exc))
  raise
finally:
  record["completed_at"] = datetime.now(timezone.utc).isoformat()
  (evidence / (phase + ".json")).write_text(
      json.dumps(record, indent=2) + "\n", encoding="utf-8"
  )
  print(json.dumps(record), flush=True)
