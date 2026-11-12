"""dashboard.sh re-execs into new code after update.sh replaces a watched file."""
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def run_reload(tmp_path, change):
    """Run the dashboard's reload block against temp copies; return its output."""
    # The reload block, pointed at temp files instead of /usr/local/scripts.
    source = (ROOT / "clients/linux/dashboard.sh").read_text()
    block = re.search(r"^_dash_files=.*?^}\n", source, re.S | re.M)[0]
    block = block.replace("/usr/local/scripts", str(tmp_path))
    (tmp_path / "dashboard.sh").write_text("echo RELOADED\n")
    (tmp_path / "common.sh").write_text("true\n")
    (tmp_path / "ini-parser.sh").write_text("true\n")
    # Portable `stat -c %Y` (macOS stat has no -c): print each file's mtime.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "stat"
    stub.write_text(f"#!{sys.executable}\nimport os,sys\n"
                    "for f in sys.argv[3:]:\n"
                    "    os.path.exists(f) and print(int(os.stat(f).st_mtime))\n")
    stub.chmod(0o755)
    script = f"{block}\n{change}\nreload_if_changed\necho STILL_OLD\n"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env).stdout


def test_reloads_when_a_sourced_file_changes(tmp_path):
    out = run_reload(tmp_path, f"touch -t 203001010000 {tmp_path}/common.sh")
    assert out.strip() == "RELOADED"


def test_keeps_running_when_nothing_changed(tmp_path):
    assert run_reload(tmp_path, "").strip() == "STILL_OLD"


def test_keeps_old_code_while_a_new_copy_does_not_parse(tmp_path):
    change = (f"echo 'if then' > {tmp_path}/common.sh; "
              f"touch -t 203001010000 {tmp_path}/common.sh")
    assert run_reload(tmp_path, change).strip() == "STILL_OLD"
