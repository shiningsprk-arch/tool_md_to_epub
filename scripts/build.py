# -*- coding: utf-8 -*-
"""推荐的工具包构建入口：清理字节码 → 调 mytool build → 校验 zip 形状。

先清掉工作树里的 `__pycache__`/`*.pyc` 再打包，再对产物做形状校验：根目录必须有
`manifest.json`、不得出现 `__pycache__`/`.pyc`/`.DS_Store`/越界顶层条目、必需文件齐全；
不通过就非零退出。目的是让"包里混进杂物"这类问题在构建期就暴露，而不是等到安装时才怪。

用法：python scripts/build.py [--keep-dist]
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_ROOT_ENTRIES = ("manifest.json", "icon.png", "backend", "frontend")
FORBIDDEN_PARTS = ("__pycache__", ".pyc", ".DS_Store", ".porting")


def clean_bytecode():
    removed = 0
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        if ".git" in dirpath or "dist" in dirpath:
            continue
        for name in list(dirnames):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)
                dirnames.remove(name)
                removed += 1
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                os.remove(os.path.join(dirpath, name))
                removed += 1
    print("清理字节码：移除 %d 项" % removed)


def run_mytool_build():
    cmd = ["npx", "--yes", "mybooks-tools-builder@latest", "build"]
    print("运行：%s" % " ".join(cmd))
    result = subprocess.run(cmd, cwd=REPO_ROOT, shell=(os.name == "nt"))
    if result.returncode != 0:
        sys.exit(result.returncode)


def verify_zip():
    dist_dir = os.path.join(REPO_ROOT, "dist")
    zips = [f for f in os.listdir(dist_dir) if f.endswith(".zip")]
    if len(zips) != 1:
        sys.exit("dist/ 下应恰好有一个 zip，实际：%s" % zips)
    zip_path = os.path.join(dist_dir, zips[0])

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

    errors = []
    if "manifest.json" not in names:
        errors.append("zip 根目录没有 manifest.json（宿主只读归档根）")
    for entry in names:
        head = entry.split("/")[0]
        if head not in PKG_ROOT_ENTRIES:
            errors.append("出现不该在包里的顶层条目：%s" % entry)
        if any(part in entry for part in FORBIDDEN_PARTS):
            errors.append("出现应排除的文件：%s" % entry)
    for required in ("icon.png", "backend/tool.py", "frontend/index.html"):
        if required not in names:
            errors.append("缺少必需文件：%s" % required)

    if errors:
        print("✗ 产物校验失败：")
        for line in errors:
            print("  - %s" % line)
        sys.exit(1)

    with open(zip_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    print("✔ 产物校验通过：%s（%d 个条目）" % (os.path.relpath(zip_path, REPO_ROOT), len(names)))
    print("  sha256: %s" % digest)
    return zip_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-dist", action="store_true", help="保留上一次 dist 产物")
    args = parser.parse_args()

    if not args.keep_dist:
        shutil.rmtree(os.path.join(REPO_ROOT, "dist"), ignore_errors=True)
    clean_bytecode()
    run_mytool_build()
    verify_zip()


if __name__ == "__main__":
    main()
