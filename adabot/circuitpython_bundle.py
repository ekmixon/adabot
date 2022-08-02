# The MIT License (MIT)
#
# Copyright (c) 2017 Scott Shawcroft for Adafruit Industries
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

""" Checks each library in the CircuitPython Bundles for updates.
    If updates are found the bundle is updated, updates are pushed to the
    remote, and a new release is made.
"""

from datetime import date
from io import StringIO
import os
import shlex
import subprocess

import redis as redis_py

import sh
from sh.contrib import git

from adabot import github_requests as github
from adabot.lib import common_funcs

REDIS = None
if "GITHUB_WORKSPACE" in os.environ:
    REDIS = redis_py.StrictRedis(port=os.environ["REDIS_PORT"])
else:
    REDIS = redis_py.StrictRedis()

BUNDLES = ["Adafruit_CircuitPython_Bundle", "CircuitPython_Community_Bundle"]


def fetch_bundle(bundle, bundle_path):
    """Clones `bundle` to `bundle_path`"""
    if not os.path.isdir(bundle_path):
        os.makedirs(bundle_path, exist_ok=True)
        if "GITHUB_WORKSPACE" in os.environ:
            git_url = (
                "https://"
                + os.environ["ADABOT_GITHUB_ACCESS_TOKEN"]
                + "@github.com/adafruit/"
            )
            git.clone("-o", "adafruit", git_url + bundle + ".git", bundle_path)
        else:
            git.clone(
                "-o",
                "adafruit",
                f"https://github.com/adafruit/{bundle}.git",
                bundle_path,
            )

    working_directory = os.getcwd()
    os.chdir(bundle_path)
    git.pull()
    git.submodule("init")
    git.submodule("update")
    os.chdir(working_directory)


# pylint: disable=too-many-locals
def check_lib_links_md(bundle_path):
    """Checks and updates the `circuitpython_library_list` Markdown document
    located in the Adafruit CircuitPython Bundle.
    """
    if "Adafruit_CircuitPython_Bundle" not in bundle_path:
        return []
    submodules_list = sorted(
        common_funcs.get_bundle_submodules(), key=lambda module: module[1]["path"]
    )

    lib_count = len(submodules_list)
    # used to generate commit message by comparing new libs to current list
    try:
        with open(
            os.path.join(bundle_path, "circuitpython_library_list.md"), "r"
        ) as lib_list:
            read_lines = lib_list.read().splitlines()
    except OSError:
        read_lines = []

    write_drivers = []
    write_helpers = []
    updates_made = []
    for submodule in submodules_list:
        url = submodule[1]["url"]
        url_name = url[
            url.rfind("/")
            + 1 : (url.rfind(".") if url.rfind(".") > url.rfind("/") else len(url))
        ]
        pypi_name = ""
        if common_funcs.repo_is_on_pypi({"name": url_name}):
            pypi_name = f' ([PyPi](https://pypi.org/project/{url_name.replace("_", "-").lower()}))'

        docs_name = ""
        if docs_link := common_funcs.get_docs_link(bundle_path, submodule):
            docs_name = f" \([Docs]({docs_link}))"  # pylint: disable=anomalous-backslash-in-string
        title = url_name.replace("_", " ")
        list_line = "* [{0}]({1}){2}{3}".format(title, url, pypi_name, docs_name)
        if list_line not in read_lines:
            updates_made.append(url_name)
        if "drivers" in submodule[1]["path"]:
            write_drivers.append(list_line)
        elif "helpers" in submodule[1]["path"]:
            write_helpers.append(list_line)

    lib_list_header = [
        "# Adafruit CircuitPython Libraries",
        (
            "![Blinka Reading](https://raw.githubusercontent.com/adafruit/circuitpython-weekly-"
            "newsletter/gh-pages/assets/archives/22_1023blinka.png)"
        ),
        "Here is a listing of current Adafruit CircuitPython Libraries.",
        f"There are {lib_count} libraries available.\n",
        "## Drivers:\n",
    ]

    with open(
        os.path.join(bundle_path, "circuitpython_library_list.md"), "w"
    ) as md_file:
        md_file.write("\n".join(lib_list_header))
        for line in sorted(write_drivers):
            md_file.write(line + "\n")
        md_file.write("\n## Helpers:\n")
        for line in sorted(write_helpers):
            md_file.write(line + "\n")

    return updates_made


class Submodule:
    """Context managing class to use with git submodules."""

    def __init__(self, directory):
        self.directory = directory
        self.original_directory = os.path.abspath(os.getcwd())

    def __enter__(self):
        os.chdir(self.directory)

    def __exit__(self, exc_type, exc_value, traceback):
        os.chdir(self.original_directory)


def commit_to_tag(repo_path, commit):
    """Fetch the tag for `commit`."""
    with Submodule(repo_path):
        try:
            output = StringIO()
            git.describe("--tags", "--exact-match", commit, _out=output)
            commit = output.getvalue().strip()
        except sh.ErrorReturnCode_128:
            pass
    return commit


def repo_version():
    """The version as defined by the tag."""
    version = StringIO()
    try:
        git.describe("--tags", "--exact-match", _out=version)
    except sh.ErrorReturnCode_128:
        git.log(pretty="format:%h", n=1, _out=version)

    return version.getvalue().strip()


def repo_sha():
    """The SHA of the repo."""
    version = StringIO()
    git.log(pretty="format:%H", n=1, _out=version)
    return version.getvalue().strip()


def repo_remote_url(repo_path):
    """The URL for the remote branch."""
    with Submodule(repo_path):
        output = StringIO()
        git.remote("get-url", "origin", _out=output)
        return output.getvalue().strip()


def update_bundle(bundle_path):
    """Process all libraries in the bundle, and update their version if necessary."""
    working_directory = os.path.abspath(os.getcwd())
    os.chdir(bundle_path)
    git.submodule("foreach", "git", "fetch")
    # Regular release tags are 'x.x.x'. Exclude tags that are alpha or beta releases.
    # They will contain a '-' in the tag, such as '3.0.0-beta.5'.
    # --exclude must be before --tags.
    # sh fails to find the subcommand so we use subprocess.
    subprocess.run(
        shlex.split(
            "git submodule foreach 'git checkout -q "
            "`git rev-list --exclude='*-*' --tags --max-count=1`'"
        ),
        stdout=subprocess.DEVNULL,
    )
    status = StringIO()
    git.status("--short", _out=status)
    updates = []
    if status := status.getvalue().strip():
        for status_line in status.split("\n"):
            action, directory = status_line.split()
            if directory.endswith("library_list.md"):
                continue
            if action != "M" or not directory.startswith("libraries"):
                raise RuntimeError("Unsupported updates")

            # Compute the tag difference.
            diff = StringIO()
            git.diff("--submodule=log", directory, _out=diff)
            diff_lines = diff.getvalue().split("\n")
            commit_range = diff_lines[0].split()[2]
            commit_range = commit_range.strip(":").split(".")
            old_commit = commit_to_tag(directory, commit_range[0])
            new_commit = commit_to_tag(directory, commit_range[-1])
            url = repo_remote_url(directory)
            summary = "\n".join(diff_lines[1:-1])
            updates.append((url[:-4], old_commit, new_commit, summary))
    os.chdir(working_directory)
    if lib_list_updates := check_lib_links_md(bundle_path):
        updates.append(
            (
                "https://github.com/adafruit/Adafruit_CircuitPython_Bundle/"
                "circuitpython_library_list.md",
                "NA",
                "NA",
                f'  > Added the following libraries: {", ".join(lib_list_updates)}',
            )
        )


    return updates


def commit_updates(bundle_path, update_info):
    """Commit changes to `bundle_path` using `update_info` for the commit message."""
    working_directory = os.path.abspath(os.getcwd())
    message = [f"Automated update by Adabot (adafruit/adabot@{repo_version()})"]
    os.chdir(bundle_path)
    for url, old_commit, new_commit, summary in update_info:
        url_parts = url.split("/")
        user, repo = url_parts[-2:]
        summary = summary.replace("#", f"{user}/{repo}#")
        message.append(f"Updating {url} to {new_commit} from {old_commit}:\n{summary}")
    message = "\n\n".join(message)
    git.add(".")
    git.commit(message=message)
    os.chdir(working_directory)


def push_updates(bundle_path):
    """Push bundle updates to the remote."""
    working_directory = os.path.abspath(os.getcwd())
    os.chdir(bundle_path)
    git.push()
    os.chdir(working_directory)


def get_contributors(repo, commit_range):
    """Get contributors to `repo` for the `commit_range`."""
    output = StringIO()
    try:
        git.log("--pretty=tformat:%H,%ae,%ce", commit_range, _out=output)
    except sh.ErrorReturnCode_128:
        print("Skipping contributors for:", repo)
    output = output.getvalue().strip()
    contributors = {}
    if not output:
        return contributors
    for log_line in output.split("\n"):
        sha, author_email, committer_email = log_line.split(",")
        author = REDIS.get(f"github_username:{author_email}")
        committer = REDIS.get(f"github_username:{committer_email}")
        if not author or not committer:
            github_commit_info = github.get(f"/repos/{repo}/commits/{sha}")
            github_commit_info = github_commit_info.json()
            if github_commit_info["author"]:
                author = github_commit_info["author"]["login"]
                REDIS.set(f"github_username:{author_email}", author)
            if github_commit_info["committer"]:
                committer = github_commit_info["committer"]["login"]
                REDIS.set(f"github_username:{committer_email}", committer)
        else:
            author = author.decode("utf-8")
            committer = committer.decode("utf-8")

        if committer_email == "noreply@github.com":
            committer = None
        if author and author not in contributors:
            contributors[author] = 0
        if committer and committer not in contributors:
            contributors[committer] = 0
        if author:
            contributors[author] += 1
        if committer and committer != author:
            contributors[committer] += 1
    return contributors


def repo_name(url):
    """Strips off .git and splits on /"""
    if url.endswith(".git"):
        url = url[:-4]
    url = url.split("/")
    return f"{url[-2]}/{url[-1]}"


# TODO: turn `master_list` into a set()?
def add_contributors(master_list, additions):
    """Adds contributors to `master_list` if not already in the list."""
    for contributor in additions:
        if contributor not in master_list:
            master_list[contributor] = 0
        master_list[contributor] += additions[contributor]


# pylint: disable=too-many-locals,too-many-branches,too-many-statements
def new_release(bundle, bundle_path):
    """Creates a new release for `bundle`."""
    working_directory = os.path.abspath(os.getcwd())
    os.chdir(bundle_path)
    print(bundle)
    current_release = github.get(f"/repos/adafruit/{bundle}/releases/latest")
    last_tag = current_release.json()["tag_name"]
    contributors = get_contributors(f"adafruit/{bundle}", f"{last_tag}..")
    added_submodules = []
    updated_submodules = []
    repo_links = {}

    output = StringIO()
    git.diff("--submodule=short", f"{last_tag}..", _out=output)
    output = output.getvalue().strip()
    if not output:
        print("Everything is already released.")
        return
    current_submodule = None
    current_index = None
    # pylint: disable=no-else-continue
    for line in output.split("\n"):
        if line.startswith("diff"):
            current_submodule = line.split()[-1][len("b/") :]
            continue
        elif "index" in line:
            current_index = line
            continue
        elif not line.startswith("+Subproject"):
            continue

        # We have a candidate submodule change.
        directory = current_submodule
        commit_range = current_index.split()[1]
        library_name = directory.split("/")[-1]
        if commit_range.startswith("0000000"):
            added_submodules.append(library_name)
            commit_range = commit_range.split(".")[-1]
        elif commit_range.endswith("0000000"):
            # For now, skip documenting deleted modules.
            continue
        else:
            updated_submodules.append(library_name)

        repo_url = repo_remote_url(directory)

        new_commit = commit_range.split(".")[-1]
        release_tag = commit_to_tag(directory, new_commit)
        with Submodule(directory):
            submodule_contributors = get_contributors(repo_name(repo_url), commit_range)
            add_contributors(contributors, submodule_contributors)
        repo_links[library_name] = f"{repo_url[:-4]}/releases/{release_tag}"

    release_description = []
    if added_submodules:
        additions = [
            f"[{library}]({repo_links[library]})"
            for library in added_submodules
        ]

        release_description.append("New libraries: " + ", ".join(additions))

    if updated_submodules:
        updates = [
            f"[{library}]({repo_links[library]})"
            for library in updated_submodules
        ]

        release_description.append("Updated libraries: " + ", ".join(updates))

    release_description.append("")

    contributors = sorted(contributors, key=contributors.__getitem__, reverse=True)
    contributors = [f"@{x}" for x in contributors]

    release_description.extend(
        (
            "As always, thank you to all of our contributors: "
            + ", ".join(contributors),
            "\n--------------------------\n",
            "The libraries in each release are compiled for all recent major versions of CircuitPython."
            " Please download the one that matches the major version of your CircuitPython. For example"
            ", if you are running 6.0.0 you should download the `6.x` bundle.\n",
            "To install, simply download the matching zip file, unzip it, and selectively copy the"
            " libraries you would like to install into the lib folder on your CIRCUITPY drive. This is"
            " especially important for non-express boards with limited flash, such as the"
            " [Trinket M0](https://www.adafruit.com/product/3500),"
            " [Gemma M0](https://www.adafruit.com/product/3501) and"
            " [Feather M0 Basic](https://www.adafruit.com/product/2772).",
        )
    )

    release = {
        "tag_name": "{0:%Y%m%d}".format(date.today()),
        "target_commitish": repo_sha(),
        "name": "{0:%B} {0:%d}, {0:%Y} auto-release".format(date.today()),
        "body": "\n".join(release_description),
        "draft": False,
        "prerelease": False,
    }

    print(f'Releasing {release["tag_name"]}')
    print(release["body"])
    response = github.post(f"/repos/adafruit/{bundle}/releases", json=release)
    if not response.ok:
        print("Failed to create release")
        print(release)
        print(response.request.url)
        print(response.text)

    os.chdir(working_directory)


if __name__ == "__main__":
    bundles_dir = os.path.abspath(".bundles")
    if "GITHUB_WORKSPACE" in os.environ:
        git.config("--global", "user.name", "adabot")
        git.config("--global", "user.email", os.environ["ADABOT_EMAIL"])
    for cp_bundle in BUNDLES:
        bundle_dir = os.path.join(bundles_dir, cp_bundle)
        try:
            fetch_bundle(cp_bundle, bundle_dir)
            if updates_needed := update_bundle(bundle_dir):
                commit_updates(bundle_dir, updates_needed)
                push_updates(bundle_dir)
            new_release(cp_bundle, bundle_dir)
        except RuntimeError as e:
            print("Failed to update and release:", cp_bundle)
            print(e)
