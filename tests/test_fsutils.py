import subprocess
import time
from pathlib import Path

from command_center.projects import fsutils


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_git_repo(root: Path) -> None:
    _git(["init"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)


# --- walk_project_files -----------------------------------------------------


def test_walk_project_files_finds_files_recursively(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("y")

    files = fsutils.walk_project_files(tmp_path)

    names = {f.name for f in files}
    assert names == {"a.py", "b.py"}


def test_walk_project_files_skips_dot_dirs_and_noise_dirs(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("x")
    for noisy in (".git", "node_modules", "__pycache__", "venv"):
        d = tmp_path / noisy
        d.mkdir()
        (d / "hidden.txt").write_text("nope")

    files = fsutils.walk_project_files(tmp_path)

    names = {f.name for f in files}
    assert names == {"keep.py"}


def test_walk_project_files_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert fsutils.walk_project_files(tmp_path / "does-not-exist") == []


def test_walk_project_files_excludes_runtime_artifact_extensions(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x")
    for noisy in ("state.db", "app.log", "cache.sqlite", "data.sqlite3"):
        (tmp_path / noisy).write_text("nope")

    files = fsutils.walk_project_files(tmp_path)

    names = {f.name for f in files}
    assert names == {"app.py"}


# --- signal-quality regression: a runtime artifact must never beat a real
# source file for "most recently touched", even when it genuinely has the
# newer mtime (a live app's own .db is written on every run) ------------------


def test_last_modified_file_ignores_db_even_when_it_is_newer(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')")
    time.sleep(0.05)
    # Written after main.py, so by raw mtime alone it would "win" — the
    # whole point of the exclusion is that it must not.
    (tmp_path / "command_center.db").write_text("binary-ish content")

    result = fsutils.last_modified_file(tmp_path)

    assert result["name"] == "main.py"


# --- last_modified_file ------------------------------------------------------


def test_last_modified_file_picks_the_newest(tmp_path: Path) -> None:
    (tmp_path / "old.py").write_text("x")
    time.sleep(0.05)
    (tmp_path / "new.py").write_text("y")

    result = fsutils.last_modified_file(tmp_path)

    assert result["name"] == "new.py"


def test_last_modified_file_empty_dir_returns_none(tmp_path: Path) -> None:
    assert fsutils.last_modified_file(tmp_path) is None


# --- last_commit_info ---------------------------------------------------------


def test_last_commit_info_returns_none_for_non_git_dir(tmp_path: Path) -> None:
    assert fsutils.last_commit_info(tmp_path) is None


def test_last_commit_info_returns_message_and_timestamp(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "f.txt").write_text("hello")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "Initial commit"], tmp_path)

    result = fsutils.last_commit_info(tmp_path)

    assert result is not None
    assert result["message"] == "Initial commit"
    assert result["timestamp"] is not None


def test_last_commit_info_git_dir_with_no_commits_returns_none(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    assert fsutils.last_commit_info(tmp_path) is None


# --- build_file_tree -----------------------------------------------------------


def test_build_file_tree_nests_directories(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("y")

    tree = fsutils.build_file_tree(tmp_path)

    names = {c["name"] for c in tree["children"]}
    assert names == {"a.py", "sub"}
    sub_node = next(c for c in tree["children"] if c["name"] == "sub")
    assert sub_node["type"] == "dir"
    assert sub_node["children"][0]["name"] == "b.py"


def test_build_file_tree_empty_dir_returns_none(tmp_path: Path) -> None:
    assert fsutils.build_file_tree(tmp_path) is None


def test_build_file_tree_missing_dir_returns_none(tmp_path: Path) -> None:
    assert fsutils.build_file_tree(tmp_path / "nope") is None


def test_build_file_tree_excludes_noise_dirs(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("x")
    noisy = tmp_path / "__pycache__"
    noisy.mkdir()
    (noisy / "x.pyc").write_text("compiled")

    tree = fsutils.build_file_tree(tmp_path)

    names = {c["name"] for c in tree["children"]}
    assert names == {"keep.py"}


def test_build_file_tree_excludes_gitignored_exact_filename(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("credentials.json\n")
    (tmp_path / "credentials.json").write_text("secret")
    (tmp_path / "keep.py").write_text("x")

    tree = fsutils.build_file_tree(tmp_path)

    names = {c["name"] for c in tree["children"]}
    assert "credentials.json" not in names
    assert "keep.py" in names


def test_build_file_tree_excludes_gitignored_directory_and_its_contents(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("secrets/\n")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "api_key.txt").write_text("secret")
    (tmp_path / "keep.py").write_text("x")

    tree = fsutils.build_file_tree(tmp_path)

    names = {c["name"] for c in tree["children"]}
    assert "secrets" not in names
    assert "keep.py" in names


def test_build_file_tree_excludes_gitignored_glob_pattern(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.env\n")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "keep.py").write_text("x")

    tree = fsutils.build_file_tree(tmp_path)

    names = {c["name"] for c in tree["children"]}
    assert ".env" not in names
    assert "keep.py" in names


def test_build_file_tree_dotfile_not_gitignored_still_appears(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("credentials.json\n")
    (tmp_path / ".env.example").write_text("SECRET=")
    (tmp_path / "keep.py").write_text("x")

    tree = fsutils.build_file_tree(tmp_path)

    names = {c["name"] for c in tree["children"]}
    assert ".env.example" in names


def test_build_file_tree_no_gitignore_present_excludes_nothing_extra(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text("secret")

    tree = fsutils.build_file_tree(tmp_path)

    names = {c["name"] for c in tree["children"]}
    assert "credentials.json" in names


def test_build_file_tree_sets_file_category(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x")
    (tmp_path / "config.toml").write_text("x")
    (tmp_path / "README.md").write_text("x")
    (tmp_path / "data.bin").write_text("x")

    tree = fsutils.build_file_tree(tmp_path)

    by_name = {c["name"]: c["category"] for c in tree["children"]}
    assert by_name["app.py"] == "code"
    assert by_name["config.toml"] == "config"
    assert by_name["README.md"] == "doc"
    assert by_name["data.bin"] is None


def test_build_file_tree_truncates_past_max_entries(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"file{i}.txt").write_text("x")

    tree = fsutils.build_file_tree(tmp_path, max_entries=3)

    names = [c["name"] for c in tree["children"]]
    assert "…truncated" in names
    assert len(names) <= 4  # 3 real entries + the truncation marker


# --- count_files -----------------------------------------------------------


def test_count_files_none_tree_is_zero() -> None:
    assert fsutils.count_files(None) == 0


def test_count_files_counts_nested_files_not_directories(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("y")
    (sub / "c.py").write_text("z")

    tree = fsutils.build_file_tree(tmp_path)

    assert fsutils.count_files(tree) == 3


def test_count_files_excludes_the_truncated_placeholder(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"file{i}.txt").write_text("x")

    tree = fsutils.build_file_tree(tmp_path, max_entries=3)

    assert fsutils.count_files(tree) == 3  # not 4 — the "…truncated" leaf isn't a real file
