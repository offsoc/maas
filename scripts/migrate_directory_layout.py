#!/usr/bin/env python3

import argparse
from collections import Counter
import enum
import io
from pathlib import Path
from shutil import which
import subprocess
import sys
from typing import (
    Any,
    Callable,
    Iterator,
    Literal,
    TypeAlias,
)

from redbaron import RedBaron  # type: ignore
from redbaron.nodes import ClassNode, CommentNode, FromImportNode  # type: ignore

MigrationOp = enum.Enum("MigrationOp", "NULL MOVE CREATE DELETE")

MigrationMoveOp: TypeAlias = tuple[Literal[MigrationOp.MOVE], Path, Path]
MigrationCreateOp: TypeAlias = tuple[Literal[MigrationOp.CREATE], Path]
MigrationDeleteOp: TypeAlias = tuple[Literal[MigrationOp.DELETE], Path]
MigrationNullOp: TypeAlias = tuple[Literal[MigrationOp.NULL], Path]
MigrationOps: TypeAlias = list[
    MigrationMoveOp | MigrationCreateOp | MigrationDeleteOp | MigrationNullOp
]


IGNORED_DIRS = (
    "maas-offline-docs",
    "maasui",
    "host-info",
    "maas.egg-info",
    "__pycache__",
)

TARGET_ROOT = Path(__file__).parent.parent / "src"

API_HANDLER_DIR = TARGET_ROOT / "maasserver" / "api"
FORMS_DIR = TARGET_ROOT / "maasserver" / "forms"
MODEL_DIRS = {
    TARGET_ROOT / "maasserver" / "models",
    TARGET_ROOT / "metadataserver" / "models",
}
WEBSOCKET_HANDLER_DIR = TARGET_ROOT / "maasserver" / "websockets" / "handlers"
POWER_DRIVER_DIR = TARGET_ROOT / "provisioningserver" / "drivers" / "power"
SIGNALS_DIR = TARGET_ROOT / "maasserver" / "models" / "signals"
RPC_DIRS = {
    TARGET_ROOT / "maasserver" / "rpc": "region",
    TARGET_ROOT / "maasserver" / "clusterrpc": "region",
    TARGET_ROOT / "provisioningserver" / "rpc": "rack",
}
VIEWS_DIRS = {TARGET_ROOT / "maasserver" / "views"}

global verbose
global dry_run


def verbose_print(msg: str) -> None:
    global verbose
    if verbose:
        print(msg)


def move_maasserver_migrations(file_name: Path) -> MigrationOps:
    return []


def move_metadataserver_migrations(file_name: Path) -> MigrationOps:
    return []


def move_maasperf(file_name: Path) -> MigrationOps:
    # NO-OP
    return [(MigrationOp.NULL, file_name)]


def move_websocket_base(websockets_dir: Path) -> MigrationOps:
    return [
        (
            MigrationOp.MOVE,
            websockets_dir / "base.py",
            TARGET_ROOT / "websockets" / "base.py",
        ),
        (
            MigrationOp.MOVE,
            websockets_dir / "protocol.py",
            TARGET_ROOT / "websockets" / "protocol.py",
        ),
        (
            MigrationOp.MOVE,
            websockets_dir / "websockets.py",
            TARGET_ROOT / "websockets" / "websockets.py",
        ),
        (
            MigrationOp.MOVE,
            websockets_dir / "tests/test_base.py",
            TARGET_ROOT / "websockets" / "tests" / "test_base.py",
        ),
        (
            MigrationOp.MOVE,
            websockets_dir / "tests/test_protocol.py",
            TARGET_ROOT / "websockets" / "tests" / "test_protocol.py",
        ),
        (
            MigrationOp.MOVE,
            websockets_dir / "tests/test_websockets.py",
            TARGET_ROOT / "websockets" / "tests" / "test_websockets.py",
        ),
    ]


def split_up_forms_init_file(file_name: Path) -> MigrationOps:
    return []


def split_up_models_init_file(file_name: Path) -> MigrationOps:
    return []


def _write_red_baron_file(dir_name: str, file_name: str, code: Any) -> Path:
    global dry_run

    dir_path = TARGET_ROOT / dir_name
    file_path = dir_path / file_name
    dir_path.mkdir(parents=True, exist_ok=True)

    if not dry_run:
        with file_path.open("w+") as f:
            f.write(code.dumps())
    return file_path


def _remove_unrelated_code(preserve_blocks: set[str], code: Any) -> Any:
    return code.filter(
        lambda x: type(x) != ClassNode or x.name in preserve_blocks
    )


def split_up_bmc_models(file_name: Path) -> MigrationOps:
    bmc_code_blocks = {
        "BaseBMCManager",
        "BMCManager",
        "BMC",
        "BMCRoutableRackControllerRelationship",
    }
    pod_code_blocks = {"PodManager", "Pod"}
    with (TARGET_ROOT / file_name).open() as f:
        bmc_src = RedBaron(f.read())
        # make a copy for the pod classes
        pod_src = bmc_src.copy()
    bmc_src = _remove_unrelated_code(bmc_code_blocks, bmc_src)
    pod_src = _remove_unrelated_code(pod_code_blocks, pod_src)
    return [
        (MigrationOp.DELETE, file_name),
        (MigrationOp.CREATE, _write_red_baron_file("bmc", "bmc.py", bmc_src)),
        (
            MigrationOp.CREATE,
            _write_red_baron_file("vmhost", "pod.py", pod_src),
        ),
    ]


def move_metadataserver_models_init_file(
    file_name: Path,
) -> MigrationOps:
    # TODO handle old package level loggers
    return [(MigrationOp.DELETE, file_name)]


def move_maasserver_init_file(file_name: Path) -> MigrationOps:
    return []


def move_utils_init_file(file_name: Path) -> MigrationOps:
    return []


def move_triggers_init_file(file_name: Path) -> MigrationOps:
    return [
        (
            MigrationOp.MOVE,
            file_name,
            TARGET_ROOT / "triggers" / "__init__.py",
        )
    ]


def move_maasserver_root_tests(file_name: Path) -> MigrationOps:
    return []


def move_maasserver_testing(file_name: Path) -> MigrationOps:
    return []


def drop_pluralization(
    parent: Path,
) -> Callable[[Path], MigrationOps]:
    def _inner(file_name: Path) -> MigrationOps:
        stem = file_name.stem
        if stem.endswith("s"):
            singular = file_name.with_stem(stem[:-1])
            return [
                (
                    MigrationOp.MOVE,
                    file_name,
                    create_destination(singular),
                )
            ]
        return [
            (
                MigrationOp.MOVE,
                file_name,
                create_destination(file_name),
            )
        ]

    return _inner


def split_node_model(file_name: Path) -> MigrationOps:
    node_code_blocks = {
        "NodeManager",
        "Node",
        "NodeQueriesMixin",
        "NodeQuerySet",
        "BaseNodeManager",
        "GeneralManager",
        "_clone_object",
        "get_bios_boot_from_bmc",
    }
    machine_code_blocks = {"Machine", "MachineManager"}
    device_code_blocks = {"Device", "DeviceManager"}
    controller_code_blocks = {
        "RegionControllerManager",
        "Controller",
        "RackController",
        "RegionController",
    }
    with (TARGET_ROOT / file_name).open() as f:
        node_src = RedBaron(f.read())
    # make a copy for the other components
    machine_src = node_src.copy()
    device_src = node_src.copy()
    controller_src = node_src.copy()
    node_src = _remove_unrelated_code(node_code_blocks, node_src)
    machine_src = _remove_unrelated_code(machine_code_blocks, machine_src)
    device_src = _remove_unrelated_code(device_code_blocks, device_src)
    controller_src = _remove_unrelated_code(
        controller_code_blocks, controller_src
    )
    return [
        (MigrationOp.DELETE, file_name),
        (
            MigrationOp.CREATE,
            _write_red_baron_file("node", "node.py", node_src),
        ),
        (
            MigrationOp.CREATE,
            _write_red_baron_file("machine", "machine.py", machine_src),
        ),
        (
            MigrationOp.CREATE,
            _write_red_baron_file("device", "device.py", device_src),
        ),
        (
            MigrationOp.CREATE,
            _write_red_baron_file(
                "controller", "controller.py", controller_src
            ),
        ),
    ]


def move_power_registry_file(file_name: Path) -> MigrationOps:
    return [
        (
            MigrationOp.MOVE,
            file_name,
            TARGET_ROOT / "power_drivers" / "registry.py",
        )
    ]


def move_power_test_registry_file(file_name: Path) -> MigrationOps:
    return [
        (
            MigrationOp.MOVE,
            file_name,
            TARGET_ROOT / "power_drivers" / "tests" / "test_registry.py",
        )
    ]


SPECIAL_CASE_DIRS: dict[Path, Callable[[Path], MigrationOps]] = {
    Path("maasperf"): move_maasperf,
    Path("maasserver") / "migrations": move_maasserver_migrations,
    Path("metadataserver") / "migrations": move_metadataserver_migrations,
    Path("maasserver") / "websockets": move_websocket_base,
    Path("maasserver") / "tests": move_maasserver_root_tests,
    Path("maasserver") / "testing": move_maasserver_testing,
}


SPECIAL_CASE_FILES: dict[Path, Callable[[Path], MigrationOps]] = {
    Path("maasserver") / "__init__.py": move_maasserver_init_file,
    Path("maasserver") / "forms" / "__init__.py": split_up_forms_init_file,
    Path("maasserver") / "models" / "__init__.py": split_up_models_init_file,
    Path("maasserver") / "utils" / "__init__.py": move_utils_init_file,
    Path("maasserver") / "triggers" / "__init__.py": move_triggers_init_file,
    Path("maasserver") / "models" / "bmc.py": split_up_bmc_models,
    Path("metadataserver")
    / "models"
    / "__init__.py": move_metadataserver_models_init_file,
    Path("maasserver")
    / "api"
    / "interfaces.py": drop_pluralization(Path("maasserver") / "api"),
    Path("maasserver") / "models" / "node.py": split_node_model,
    Path("provisioningserver")
    / "drivers"
    / "power"
    / "registry.py": move_power_registry_file,
    Path("provisioningserver")
    / "drivers"
    / "power"
    / "tests"
    / "test_registry.py": move_power_test_registry_file,
}


def generate_base_dir_name(root_path: Path, file_name: str) -> str:
    root = str(root_path)
    if root[-1] != "/":
        root += "/"
    base_name = file_name.replace(root, "").split(".")[0]
    if base_name.startswith("test_"):
        return base_name.replace("test_", "")
    return base_name


def create_destination(file_path: Path) -> Path:
    parent = file_path.parent
    file_name = str(file_path)
    if file_path.name == "__init__.py" and file_path.stat().st_size == 0:
        return Path()
    base_dir = TARGET_ROOT / generate_base_dir_name(parent, file_name)
    if parent.name == "tests":
        grandparent = parent.parent
        if grandparent in MODEL_DIRS:
            return base_dir / "tests" / file_path.name
        elif grandparent == API_HANDLER_DIR:
            return base_dir / "tests" / "test_api_handler.py"
        elif grandparent == WEBSOCKET_HANDLER_DIR:
            return base_dir / "tests" / "test_ws_handler.py"
        elif grandparent == FORMS_DIR:
            return base_dir / "tests" / "test_forms.py"
        elif grandparent == POWER_DRIVER_DIR:
            return base_dir / "tests" / "test_driver.py"
        elif grandparent in RPC_DIRS:
            if "maasserver" in parent.parts:
                return base_dir / "tests" / "test_region_rpc_handler.py"
            else:
                return base_dir / "tests" / "test_rack_rpc_handler.py"
    elif parent in MODEL_DIRS:
        return base_dir / file_path.name
    elif parent == API_HANDLER_DIR:
        return base_dir / "api_handler.py"
    elif parent == WEBSOCKET_HANDLER_DIR:
        return base_dir / "ws_handler.py"
    elif parent == FORMS_DIR:
        return base_dir / "forms.py"
    elif parent == POWER_DRIVER_DIR and file_path.stem == ".py":
        return base_dir / "driver.py"
    elif parent in RPC_DIRS:
        if base_dir.name == "__init__":
            rpc_dir = base_dir.with_name("rpc")
        else:
            rpc_dir = base_dir
        if "maasserver" in parent.parts:
            return rpc_dir / "region_rpc_handler.py"
        return rpc_dir / "rack_rpc_handler.py"
    return file_path


def load_layout_changes() -> MigrationOps:
    changes: MigrationOps = []

    output = subprocess.check_output(
        ["git", "ls-files"], text=True, cwd=TARGET_ROOT
    )
    for line in output.splitlines():
        file_path = Path(line)
        if (parent := file_path.parent) in SPECIAL_CASE_DIRS:
            changes += SPECIAL_CASE_DIRS.pop(parent)(parent)
        if any(part.name in IGNORED_DIRS for part in file_path.parents):
            continue
        if file_path in SPECIAL_CASE_FILES:
            changes += SPECIAL_CASE_FILES[file_path](file_path)
        else:
            changes.append(
                (
                    MigrationOp.MOVE,
                    file_path,
                    create_destination(TARGET_ROOT / file_path),
                )
            )

    return changes


def validate_unique_targets(changes: MigrationOps) -> None:
    file_count: Counter = Counter()
    for change in changes:
        match change:
            case [MigrationOp.CREATE, path]:
                file_count[path] += 1
            case [MigrationOp.MOVE, _, new]:
                file_count[new] += 1
    broken_files = []
    del file_count[Path(".")]
    for file_path, count in file_count.most_common():
        if count == 1:
            break
        broken_files.append(
            f"{file_path} is target for more than one ({count}) source file"
        )
    assert not broken_files, "\n".join(broken_files)


def move_files(changes: MigrationOps, dry_run: bool = False) -> None:
    git_cmd = which("git")
    assert git_cmd is not None, "Missing git on $PATH"
    for change in changes:
        match change:
            case [MigrationOp.CREATE, path]:
                assert not path.exists()
                path.touch()
                subprocess.check_call([git_cmd, "add", path])
            case [MigrationOp.DELETE, path]:
                assert path.exists()
                subprocess.check_call([git_cmd, "rm", path])
            case [MigrationOp.MOVE, old, new]:
                assert old.exists()
                assert not new.exists()
                subprocess.check_call([git_cmd, "mv", old, new])
            case [MigrationOp.NULL, path]:
                pass
            case _ as op:
                raise RuntimeError(f"Unknown operation: {repr(op)}")


SPECIAL_CASE_IMPORTS = {
    TARGET_ROOT / "bmc" / "bmc.py": ("maasserver.models.bmc.BMC*", "bmc.BMC*"),
    TARGET_ROOT
    / "vmhost"
    / "pod.py": ("maasserver.models.bmc.Pod*", "vmhost.Pod*"),
}


def _format_import_from_path(path: Path) -> str:
    if path.is_relative_to(TARGET_ROOT):
        path = path.relative_to(TARGET_ROOT)
    components = path.with_name(path.stem).parts
    return ".".join(components)


def _generate_imports(changes: MigrationOps) -> Iterator[tuple[str, str]]:
    for change in changes:
        match change:
            case [MigrationOp.MOVE, old, new] if new in SPECIAL_CASE_IMPORTS:
                yield SPECIAL_CASE_IMPORTS[new]
            case [MigrationOp.MOVE, old, new] if new != Path("."):
                old_import = _format_import_from_path(old)
                new_import = _format_import_from_path(new)
                if not new_import:
                    continue
                yield (old_import, new_import)


def _find_and_swap_imports(
    imports: Iterator[tuple[str, str]], f: io.TextIOWrapper
) -> Any:
    src = RedBaron(f.read())
    for import_pair in imports:
        lines = src.find_all(
            "import_node", value=lambda v: v in import_pair[0].split(".")
        )
        for line in lines:
            pass


def modify_imports(changes: MigrationOps) -> None:
    imports = _generate_imports(changes)

    def _modify(file_name: Path) -> None:
        with file_name.open() as f:
            new_src = _find_and_swap_imports(imports, f)
            f.write(new_src.dumps())

    for change in changes:
        match change:
            case [MigrationOp.MOVE, _, new]:
                _modify(new)


def diff_imports(changes: MigrationOps) -> Any:
    imports = _generate_imports(changes)

    def _diff(file_name: str) -> Any:
        with open(file_name) as f:
            new_src = _find_and_swap_imports(imports, f)
            # src = RedBaron(f.read())

    return "TODO: Diff imports"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate to new directory structure"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print out destination paths and import statement diffs, but do not actually modify files",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable verbose messaging",
    )

    args = parser.parse_args()
    verbose = args.verbose
    dry_run = args.dry_run

    changes = load_layout_changes()
    for change in changes:
        match change:
            case [MigrationOp.CREATE, target]:
                if target.is_relative_to(TARGET_ROOT):
                    target = target.relative_to(TARGET_ROOT)
                print(f"✨ {target}")
            case [MigrationOp.DELETE, target]:
                if target.is_relative_to(TARGET_ROOT):
                    target = target.relative_to(TARGET_ROOT)
                print(f"💣 {target}")
            case [MigrationOp.MOVE, old, new]:
                if new.is_relative_to(TARGET_ROOT):
                    new = new.relative_to(TARGET_ROOT)
                if old == new:
                    continue
                print(f"🚚 {old} ➡ {new}")
            case [MigrationOp.NULL, target]:
                pass

    validate_unique_targets(changes)

    if not args.dry_run:
        confirmation = input("OK to proceed?\n")
        if not confirmation.lower().startswith("y"):
            sys.exit("Aborted at user request")

        move_files(changes, dry_run)
        modify_imports(changes)
    else:
        print(diff_imports(changes), file=sys.stderr)
