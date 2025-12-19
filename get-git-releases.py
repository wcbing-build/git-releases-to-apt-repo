#!/usr/bin/env python3
import json
import logging
import os
import re
import requests
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, wait


# 全局配置变量
CONFIG = {
    "data_dir": "data",
    "deb_dir": "deb",
    "packages_dir": "packages",
    "thread": 5,
    "dry_run": False,
}
tag_lock = threading.Lock()
# 日志等级，若需要展示每次请求结果请使用 INFO 等级
logging.basicConfig(level=logging.INFO)


# 读取 JSON 文件
def read_json(filename: str) -> dict:
    try:
        with open(os.path.join(CONFIG["data_dir"], filename), "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.error(e)
        return {}


# 获取最新标签
def latest_releases_tag(repo_url: str) -> str:
    url = f"{repo_url}/releases/latest"
    try:
        location = requests.head(url).headers.get("Location", "")
        match = re.search(r".*releases/tag/([^/]+)", location)
        return match.group(1) if match else ""
    except requests.RequestException as e:
        logging.error(e)
        return ""


def format_release_filename(template: str, releases_tag: str) -> str:
    # https://www.debian.org/doc/manuals/debmake-doc/ch06.zh-cn.html#name-version
    # 参考文档中 Upstream version 正则表达式获取版本号，即从第一个数字开始。
    vpattern = "[0-9][-+.:~a-z0-9A-Z]*"
    version = match.group() if (match := re.search(vpattern, releases_tag)) else ""

    return template.format(
        releases_tag=releases_tag,  # 若存在则用完整 tag 替换
        version=version,  # 若存在则用 version 替换
    )


# 下载文件
def download(url: str, file_path: str) -> bool:
    # 检查是否为 dry-run 模式
    method = requests.head if CONFIG["dry_run"] else requests.get
    response = method(url, stream=True, allow_redirects=True)
    if response.status_code != 200:
        logging.error(f"Can't download {url} because received {response.status_code}")
        return False

    if CONFIG["dry_run"]:
        logging.info(f"Dry-run download: {url}")
    else:
        logging.info(f"Downloading: {url}")
        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
    return True


def scan(name, arch, url, file_path) -> bool:
    scan_process = subprocess.run(
        ["apt-ftparchive", "packages", file_path], capture_output=True
    )
    package = scan_process.stdout.decode()
    package = re.sub(
        r"^(Filename: ).*", f"\\1{url}", package, flags=re.MULTILINE
    )  # 替换 Filename 开头的行

    package_file_path = os.path.join(CONFIG["packages_dir"], arch, f"{name}.package")

    try:
        with open(package_file_path, "w") as f:
            f.write(package)
            return True
    except IOError as e:
        logging.error(f"Failed to write package file for {name}: {e}")
        return False


# 检查版本并下载新版本文件
def check(name: str, repo: dict, tag_list: dict) -> None:
    if "site" in repo:
        repo_url = os.path.join(repo["site"], repo["repo"])
    else:
        # 默认认为是 GitHub 仓库地址
        repo_url = os.path.join("https://github.com", repo["repo"])
    releases_tag = latest_releases_tag(repo_url)
    if not releases_tag:
        logging.error(f"Can't get latest releases tag of {name}")
        return
    logging.info(f"{name} = {releases_tag}")

    # 判断是否需要更新
    local_tag = tag_list.get(name, "")
    if not releases_tag or local_tag == releases_tag:
        return

    name = repo["package_name"] if "package_name" in repo else name
    for arch, template in repo["file_list"].items():
        # 确定本地文件目录并确保目录存在
        app_dir = os.path.join(CONFIG["deb_dir"], name)
        os.makedirs(app_dir, exist_ok=True)
        # 得到 Releases 中的文件名
        release_filename = format_release_filename(template, releases_tag)
        url = f"{repo_url}/releases/download/{releases_tag}/{release_filename}"
        file_path = os.path.join(app_dir, release_filename)

        # download and scan
        logging.info(f"Downloading {name}:{arch} ({releases_tag})")
        os.makedirs(os.path.join(CONFIG["deb_dir"], arch), exist_ok=True)
        if not download(url, file_path):
            continue
        logging.info(f"Downloaded {name}:{arch} ({releases_tag})")
        os.makedirs(os.path.join(CONFIG["packages_dir"], arch), exist_ok=True)
        if not scan(name, arch, url, file_path):
            continue
        # 判断是否是新添加应用
        if local_tag == "":
            print(f"AddNew: {name}:{arch} ({releases_tag})")
        else:
            print(f"Update: {name}:{arch} ({local_tag} -> {releases_tag})")
            # 删除旧版本文件
            old_file_path = os.path.join(
                app_dir, format_release_filename(template, local_tag)
            )
            if os.path.exists(old_file_path):
                os.remove(old_file_path)
        # 更新版本号
        with tag_lock:
            tag_list[name] = releases_tag


if __name__ == "__main__":
    git_repo_list = read_json("git-repo.json")
    tag_list = read_json("git-tag.json")
    with ThreadPoolExecutor(max_workers=CONFIG["thread"]) as executor:
        tasks = [
            executor.submit(check, name, repo, tag_list)
            for name, repo in git_repo_list.items()
        ]
        wait(tasks)
    # 保存到 git-tag.json
    with open(os.path.join(CONFIG["data_dir"], "git-tag.json"), "w") as f:
        json.dump(tag_list, f, indent=4)
