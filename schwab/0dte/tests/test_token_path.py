"""SCHWAB_TOKEN_PATH may name the token FILE or the Schwab FOLDER that holds it (a blank setting means the default) -- 2026-10-14.

The .env files of the bot machine point at ...\\schwab\\0dte (the folder); reading a folder as a file is a PermissionError on Windows."""

import os
import subprocess
import sys
from pathlib import Path

FOLDER = Path(__file__).resolve().parents[1]


def config_path(value):
    """config.SCHWAB_TOKEN_PATH as a fresh process computes it, with the folder's real .env ignored."""
    env = {k: v for k, v in os.environ.items() if k != "SCHWAB_TOKEN_PATH"}
    if value is not None:
        env["SCHWAB_TOKEN_PATH"] = value
    out = subprocess.run([sys.executable, "-c", "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import config; "
                          "print(config.SCHWAB_TOKEN_PATH)"], cwd=str(FOLDER), env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_a_folder_means_the_token_json_inside_it(tmp_path):
    folder = tmp_path / "0dte"
    folder.mkdir()
    assert config_path(str(folder)) == str(folder / "token.json")


def test_a_file_path_even_one_that_does_not_exist_yet_is_left_alone(tmp_path):
    target = tmp_path / "somewhere" / "my-token.json"
    assert config_path(str(target)) == str(target)


def test_a_blank_setting_means_the_default():
    default = config_path(None)
    assert default.endswith("token.json")
    assert config_path("") == default and config_path("   ") == default
