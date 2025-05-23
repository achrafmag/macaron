# Copyright (c) 2022 - 2025, Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/.

"""This module provides methods to perform generic actions on Git URLS."""


import logging
import os
import re
import string
import subprocess  # nosec B404
import urllib.parse
from configparser import ConfigParser
from pathlib import Path

from git import GitCommandError
from git.objects import Commit
from git.repo import Repo
from packaging import version
from pydriller.git import Git

from macaron.config.defaults import defaults
from macaron.config.global_config import global_config
from macaron.environment_variables import get_patched_env
from macaron.errors import CloneError, GitTagError

logger: logging.Logger = logging.getLogger(__name__)


GIT_REPOS_DIR = "git_repos"
"""The directory in the output dir to store all cloned repositories."""


def parse_git_branch_output(content: str) -> list[str]:
    """Return the list of branch names from a string that has a format similar to the output of ``git branch --list``.

    Parameters
    ----------
    content : str
        The raw output as string from the ``git branch`` command.

    Returns
    -------
    list[str]
        The list of strings where each string is a branch element from the raw output.

    Examples
    --------
    >>> from pprint import pprint
    >>> content = '''
    ... * (HEAD detached at 7fc81f8)
    ...   master
    ...   remotes/origin/HEAD -> origin/master
    ...   remotes/origin/master
    ...   remotes/origin/v2.dev
    ...   remotes/origin/v3.dev
    ... '''
    >>> pprint(parse_git_branch_output(content))
    ['(HEAD detached at 7fc81f8)',
     'master',
     'remotes/origin/HEAD -> origin/master',
     'remotes/origin/master',
     'remotes/origin/v2.dev',
     'remotes/origin/v3.dev']
    """
    git_branch_output_lines = content.splitlines()
    branches = []
    for line in git_branch_output_lines:
        # The ``*`` symbol will appear next to the branch name where HEAD is currently on.
        # Branches in git cannot have ``*`` in its name so we can safely replace without tampering with its actual name.
        # https://git-scm.com/docs/git-check-ref-format
        branch = line.replace("*", "").strip()

        # Ignore elements that contain only whitespaces. This is because the raw content of git branch
        # can have extra new line at the end, which can be picked up as an empty element in `git_branch_output_lines`.
        if len(branch) == 0:
            continue

        branches.append(branch)

    return branches


def get_branches_containing_commit(git_obj: Git, commit: str, remote: str = "origin") -> list[str]:
    """Get the branches from a remote that contains a specific commit.

    The returned branch names will be in the form of <remote>/<branch_name>.

    Parameters
    ----------
    git_obj : Git
        The pydriller.Git wrapper object of the target repository.
    commit : str
        The hash of the commit we want to get all the branches.
    remote : str, optional
        The name of the remote to check the branches, by default "origin".

    Returns
    -------
    list[str]
        The list of branches that contains the commit.
    """
    try:
        raw_output: str = git_obj.repo.git.branch(
            "--remotes",
            "--list",
            f"{remote}/*",
            "--contains",
            commit,
        )
    except GitCommandError:
        logger.debug("Error while looking up branches that contain commit %s.", commit)
        return []

    return parse_git_branch_output(raw_output)


def check_out_repo_target(
    git_obj: Git,
    branch_name: str = "",
    digest: str = "",
    offline_mode: bool = False,
) -> bool:
    """Checkout the branch and commit specified by the user.

    This function assumes that a remote "origin" exist and checkout from that remote ONLY.

    If ``offline_mode`` is True and neither ``branch_name`` nor commit are provided, this function will not do anything
    and the HEAD commit will be analyzed. If there are uncommitted local changes, the HEAD commit will
    appear in the report but the repo with local changes will be analyzed. We leave it up to the user to decide
    whether to commit the changes or not.

    If ``branch_name`` is provided and a commit is not provided, this function will checkout that branch from origin
    remote (i.e. origin/<branch_name).

    If ``branch_name`` is not provided and a commit is provided, this function will checkout the commit directly.

    If both ``branch_name`` and a commit are provided, this function will checkout the commit directly only if that
    commit exists in the branch origin/<branch_name>. If not, this function will return False.

    For all scenarios:
    - If the checkout fails (e.g. a branch or a commit doesn't exist), this function will return
    False.
    - This function will perform a force checkout
    https://git-scm.com/docs/git-checkout#Documentation/git-checkout.txt---force

    This function supports repositories which are cloned from existing remote repositories.
    Other scenarios are not covered (e.g. a newly initiated repository).

    Parameters
    ----------
    git_obj : Git
        The pydriller.Git wrapper object of the target repository.
    branch_name : str
        The name of the branch we want to checkout.
    digest : str
        The hash of the commit that we want to checkout in the branch.
    offline_mode : bool
        If True, this function will not perform any online operation (fetch, pull).

    Returns
    -------
    bool
        True if succeed else False.
    """
    if not offline_mode and not branch_name and not digest:
        try:
            git_obj.repo.git.checkout("--force", "origin/HEAD")
        except GitCommandError:
            logger.debug("Cannot checkout the default branch at origin/HEAD")
            return False

    # The following checkout operations will be done whether offline_mode is False or not.
    if branch_name and not digest:
        try:
            git_obj.repo.git.checkout("--force", f"origin/{branch_name}")
        except GitCommandError:
            logger.debug("Cannot checkout branch %s from origin remote.", branch_name)
            return False

    if not branch_name and digest:
        try:
            git_obj.repo.git.checkout("--force", f"{digest}")
        except GitCommandError:
            logger.debug("Cannot checkout commit %s.", digest)
            return False

    if branch_name and digest:
        branches = get_branches_containing_commit(
            git_obj=git_obj,
            commit=digest,
            remote="origin",
        )

        if f"origin/{branch_name}" in branches:
            try:
                git_obj.repo.git.checkout("--force", f"{digest}")
            except GitCommandError:
                logger.debug("Cannot checkout commit %s.", digest)
                return False
        else:
            logger.error("Commit %s is not in branch %s.", digest, branch_name)
            return False

    # Further validation to make sure the git checkout operations happen as expected.
    final_head_commit: Commit = git_obj.repo.head.commit
    if not final_head_commit:
        logger.critical("Cannot get the head commit after checking out.")
        return False

    if digest and final_head_commit.hexsha != digest:
        logger.critical("The current HEAD at %s. Expect %s.", final_head_commit.hexsha, digest)
        return False

    logger.info("The HEAD commit is %s.", final_head_commit.hexsha)
    return True


def get_default_branch(git_obj: Git) -> str:
    """Return the default branch name of the target repository.

    This function does not perform any online operation. It depends on the existence of the remote
    reference ``origin/HEAD`` in the git repository. This remote reference will point to the default
    branch of the remote repository and it's usually set when the repository is first cloned with
    ``git clone <url>``.
    Therefore, this method will fail to obtain the default branch name if ``origin/HEAD`` is not
    available. An example of this case is when a repository is shallow-cloned from a non-default branch
    (e.g. ``git clone --depth=1 <url> -b some_branch``).

    Parameters
    ----------
    git_obj : Git
        The pydriller.Git wrapper object of the target repository.

    Returns
    -------
    str
        The default branch name or empty if errors.
    """
    try:
        # https://stackoverflow.com/questions/28666357/git-how-to-get-default-branch
        # This command will return origin/<default-branch-name>.
        # It can also work after we checkout a specific commit making HEAD into a detached state.
        # This is suitable for running multiple times on a repo.
        default_branch_full: str = git_obj.repo.git.rev_parse("--abbrev-ref", "origin/HEAD")
        return default_branch_full[7:]
    except GitCommandError as error:
        logger.error("Error when getting default branch. Error: %s", error)
        return ""


def is_remote_repo(path_to_repo: str) -> bool:
    """Verify if the given repository path is a remote path.

    Parameters
    ----------
    path_to_repo : str
        The path of the repository to check.

    Returns
    -------
    bool
        True if it's a remote path else return False.
    """
    # Validate the url.
    parsed_url = get_remote_vcs_url(path_to_repo)
    if parsed_url == "":
        logger.debug("URL '%s' is not valid.", path_to_repo)
        return False
    return True


def clone_remote_repo(clone_dir: str, url: str) -> Repo | None:
    """Clone the remote repository and return the `git.Repo` object for that repository.

    If there is an existing non-empty ``clone_dir``, Macaron assumes the repository has
    been cloned already and will attempt to fetch the latest changes. The fetching operation
    will prune and update all references (e.g. tags, branches) to make sure that the local
    repository is up-to-date with the repository specified by origin remote.

    We use treeless partial clone to reduce clone time, by retrieving trees and blobs lazily.
    For more details, see the following:
    - https://git-scm.com/docs/partial-clone
    - https://git-scm.com/docs/git-rev-list
    - https://github.blog/2020-12-21-get-up-to-speed-with-partial-clone-and-shallow-clone

    Parameters
    ----------
    clone_dir : str
        The directory to clone the repo to.
    url : str
        The url to clone the repository.
        Important: this can contain secrets! (e.g. cloning with GitLab token)

    Returns
    -------
    git.Repo | None
        The ``git.Repo`` object of the repository, or ``None`` if the clone directory already exists.

    Raises
    ------
    CloneError
        If the repository has not been cloned and the clone attempt fails.
    """
    # Handle the case where the repository already exists in `<output_dir>/git_repos`.
    # This could happen when multiple runs of Macaron use the same `<output_dir>`, leading to
    # Macaron attempting to clone a repository multiple times.
    # In these cases, we should not error since it may interrupt the analysis.
    if os.path.isdir(clone_dir):
        try:
            os.rmdir(clone_dir)
            logger.debug("The clone dir %s is empty. It has been deleted for cloning the repo.", clone_dir)
        except OSError:
            # Update the existing repository by running ``git fetch`` inside the existing directory.
            # The flags `--force --tags --prune --prune-tags` are used to make sure we analyze the most up-to-date
            # version of the repo.
            #   - Any modified tags in the remote repository are updated locally.
            #   - Deleted branches and tags in the remote repository are pruned from the local copy.
            # References:
            #   https://git-scm.com/docs/git-fetch
            #   https://github.com/oracle/macaron/issues/547
            try:
                git_env_patch = {
                    # Setting the GIT_TERMINAL_PROMPT environment variable to ``0`` stops
                    # ``git clone`` from prompting for login credentials.
                    "GIT_TERMINAL_PROMPT": "0",
                }
                subprocess.run(  # nosec B603
                    args=["git", "fetch", "origin", "--force", "--tags", "--prune", "--prune-tags"],
                    capture_output=True,
                    cwd=clone_dir,
                    # If `check=True` and return status code is not zero, subprocess.CalledProcessError is
                    # raised, which we don't want. We want to check the return status code of the subprocess
                    # later on.
                    check=False,
                    env=get_patched_env(git_env_patch),
                )
                return Repo(path=clone_dir)
            except (subprocess.CalledProcessError, OSError):
                logger.debug("The clone dir %s is not empty. An attempt to update it failed.")
                return None

    # Ensure that the parent directory where the repo is cloned into exists.
    parent_dir = Path(clone_dir).parent
    parent_dir.mkdir(parents=True, exist_ok=True)

    try:
        git_env_patch = {
            # Setting the GIT_TERMINAL_PROMPT environment variable to ``0`` stops
            # ``git clone`` from prompting for login credentials.
            "GIT_TERMINAL_PROMPT": "0",
        }
        result = subprocess.run(  # nosec B603
            args=["git", "clone", "--filter=tree:0", url],
            capture_output=True,
            cwd=parent_dir,
            # If `check=True` and return status code is not zero, subprocess.CalledProcessError is
            # raised, which we don't want. We want to check the return status code of the subprocess
            # later on.
            check=False,
            env=get_patched_env(git_env_patch),
        )
    except (subprocess.CalledProcessError, OSError):
        # Here, we raise from ``None`` to be extra-safe that no token is leaked.
        # We should never store or print out the captured output from the subprocess
        # because they might contain the secret-embedded URL.
        raise CloneError("Failed to clone repository.") from None

    if result.returncode != 0:
        raise CloneError(
            "Failed to clone repository: the `git clone --filter=tree:0` command exited with non-zero return code."
        )

    return Repo(path=clone_dir)


def list_remote_references(arguments: list[str], repo: str) -> str | None:
    """Retrieve references from a remote repository using Git's ``ls-remote``.

    Parameters
    ----------
    arguments: list[str]
        The arguments to pass into the command.
    repo: str
        The repository to run the command on.

    Returns
    -------
    str
        The result of the command.
    """
    try:
        result = subprocess.run(  # nosec B603
            args=["git", "ls-remote"] + arguments + [repo],
            capture_output=True,
            # By setting stdin to /dev/null and using a new session, we prevent all possible user input prompts.
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=global_config.output_path,
            check=False,
        )
    except (subprocess.CalledProcessError, OSError):
        return None

    if result.returncode != 0:
        error_string = result.stderr.decode("utf-8").strip()
        if error_string.startswith("fatal: could not read Username"):
            # Occurs when a repository cannot be accessed either because it does not exist, or it requires a login
            # that is blocked.
            logger.error("Could not access repository: %s", repo)
        else:
            logger.error("Failed to retrieve remote references from repo: %s", repo)
        return None

    return result.stdout.decode("utf-8")


def resolve_local_path(start_dir: str, local_path: str) -> str:
    """Resolve the local path and check if it's within a directory.

    This method returns an empty string if there are errors with resolving ``local_path``
    (e.g. non-existed dir, broken symlinks, etc.) or ``start_dir`` does not exist.

    Parameters
    ----------
    start_dir : str
        The directory to look for the existence of path.
    local_path: str
        The local path to resolve within start_dir.

    Returns
    -------
    str
        The resolved path in canonical form or an empty string if errors.
    """
    # Resolve the path by joining dir and path.
    # Because strict mode is enabled, if a path doesn't exist or a symlink loop
    # is encountered, OSError is raised.
    # ValueError is raised if we use both relative and absolute paths in os.path.commonpath.
    try:
        dir_real = os.path.realpath(start_dir, strict=True)
        resolve_path = os.path.realpath(os.path.join(start_dir, local_path), strict=True)
        if os.path.commonpath([resolve_path, dir_real]) != dir_real:
            return ""

        return resolve_path
    except (OSError, ValueError) as error:
        logger.error(error)
        return ""


def get_repo_name_from_url(url: str) -> str:
    """Extract the repo name of the repository from the remote url.

    Parameters
    ----------
    url : str
        The remote url of the repository.

    Returns
    -------
    str
        The name of the repository or an empty string if errors.

    Examples
    --------
    >>> get_repo_name_from_url("https://github.com/owner/repo")
    'repo'
    """
    full_name = get_repo_full_name_from_url(url)
    if not full_name:
        logger.error("Cannot extract repo name if we cannot extract the fullname of %s.", url)
        return ""

    return full_name.split("/")[1]


def get_repo_full_name_from_url(url: str) -> str:
    """Extract the full name of the repository from the remote url.

    The full name is in the form <owner>/<name>. Note that this function assumes `url` is a remote url.

    Parameters
    ----------
    url : str
        The remote url of the repository.

    Returns
    -------
    str
        The full name of the repository or an empty string if errors.
    """
    # Parse the remote url.
    parsed_url = parse_remote_url(url)
    if not parsed_url:
        logger.debug("URL '%s' is not valid.", url)
        return ""

    full_name = parsed_url.path.split(".git")[0]

    # The full name must be in org/repo format
    if len(full_name.split("/")) != 2:
        logger.error("Fullname %s extract from %s is not valid.", full_name, url)
        return ""

    return full_name


def get_repo_complete_name_from_url(url: str) -> str:
    """Return the complete name of the repo from a remote repo url.

    The complete name will be in the form ``<git_host>/org/name``.

    Parameters
    ----------
    url: str
        The remote url of the target repository.

    Returns
    -------
    str
        The unique path resolved from the remote path or an empty string if errors.

    Examples
    --------
    >>> from macaron.config.defaults import load_defaults
    >>> load_defaults("")
    True
    >>> get_repo_complete_name_from_url("https://github.com/apache/maven")
    'github.com/apache/maven'
    """
    remote_url = get_remote_vcs_url(url)
    if not remote_url:
        logger.debug("URL '%s' is not valid.", url)
        return ""

    parsed_url = parse_remote_url(remote_url)
    if not parsed_url:
        # Shouldn't happen.
        logger.critical("URL '%s' is not valid even though it has been validated.", url)
        return ""

    git_host = parsed_url.netloc
    return os.path.join(git_host, parsed_url.path.strip("/"))


def get_remote_origin_of_local_repo(git_obj: Git) -> str:
    """Get the origin remote of a repository.

    Note that this origin remote can be either a remote url or a path to a local repo.

    Parameters
    ----------
    git_obj : Git
        The pydriller.Git object of the repository.

    Returns
    -------
    str
        The origin remote path or empty if error.
    """
    try:
        remote_origin = git_obj.repo.remote("origin")
        remote_urls = [*remote_origin.urls]
        remote_urls.sort()
        valid_remote_path_set = {value for value in remote_urls if value != ""}
    except ValueError as error:
        logger.error("No origin remote discovered for the repository: %s", error)
        return ""

    # This path could be either a remote path or a local path (if the repo is cloned from another local repo or from
    # a git bundle).
    # We don't need to validate this path as we are getting it from an already cloned repository. If the path is invalid
    # it should have been caught during the preparing process of the git repository.
    remote_origin_path = valid_remote_path_set.pop()

    # Hide the GitLab OAuth token from the repo's remote.
    # This is because cloning from GitLab with an access token requires us to embed
    # the token in the URL.
    if "oauth2" in remote_origin_path:
        try:
            url_parse_result = urllib.parse.urlparse(remote_origin_path)
        except ValueError:
            logger.error("Error occurs while processing the remote URL of repo %s.", git_obj.project_name)
            return ""

        _, _, hostname = url_parse_result.netloc.rpartition("@")

        new_url_parse_result = urllib.parse.ParseResult(
            scheme=url_parse_result.scheme,
            netloc=hostname,
            path=url_parse_result.path,
            params=url_parse_result.params,
            query=url_parse_result.query,
            fragment=url_parse_result.fragment,
        )
        remote_origin_path = urllib.parse.urlunparse(new_url_parse_result)

    return remote_origin_path or ""


def clean_up_repo_path(repo_path: str) -> str:
    """Clean up the repo path.

    This method returns the repo path after cleaning up.

    Parameters
    ----------
    repo_path : str
        The repo path to clean up.

    Returns
    -------
    str
        The cleaned up repo path.
    """
    cleaned_path = repo_path.strip(" ").rstrip("/")
    return cleaned_path[:-4] if cleaned_path.endswith(".git") else cleaned_path


def get_remote_vcs_url(url: str, clean_up: bool = True) -> str:
    """Verify if the given repository path is a valid vcs.

    We support some of the patterns listed in https://git-scm.com/docs/git-clone#_git_urls.

    Parameters
    ----------
    url : str
        The path of the repository to check.
    clean_up : bool
        Set to True to clean up the returned remote url (default: True).

    Returns
    -------
    str
        The remote url to the repo or empty if the url is invalid.
    """
    parsed_result = parse_remote_url(url)
    if not parsed_result:
        return ""

    url_as_str = urllib.parse.urlunparse(parsed_result)

    if clean_up:
        return clean_up_repo_path(url_as_str)

    return url_as_str


def clean_url(url: str) -> urllib.parse.ParseResult | None:
    """Clean the passed url, removing extraneous prefixes and parsing it with urllib.

    Parameters
    ----------
    url: str
        The path to a repository.

    Returns
    -------
    ParseResult:
        The parsed URL.
    """
    try:
        # Remove prefixes, such as "scm:" and "git:".
        match = re.match(r"(?P<prefix>(.*?))(git\+http|http|ftp|ssh\+git|ssh|git@)(.)*", str(url))
        if match is None:
            return None
        cleaned_url = url.replace(match.group("prefix"), "")

        # Parse the URL string to determine how to handle it.
        return urllib.parse.urlparse(cleaned_url)
    except (ValueError, TypeError) as error:
        logger.debug(error)
        return None


def parse_remote_url(
    url: str, allowed_git_service_hostnames: list[str] | None = None
) -> urllib.parse.ParseResult | None:
    """Verify if the given repository path is a valid vcs.

    This method converts the url to a ``https://`` url and return a
    ``urllib.parse.ParseResult object`` to be consumed by Macaron.
    Note that the port number in the original url will be removed.

    Parameters
    ----------
    url: str
        The path of the repository to check.
    allowed_git_service_hostnames: list[str] | None
        The list of allowed git service hostnames.
        If this is ``None``, fall back to the  ``.ini`` configuration.
        (Default: None).

    Returns
    -------
    urllib.parse.ParseResult | None
        The parse result of the url or None if errors.

    Examples
    --------
    >>> parse_remote_url("ssh://git@github.com:7999/owner/org.git")
    ParseResult(scheme='https', netloc='github.com', path='owner/org.git', params='', query='', fragment='')
    """
    if allowed_git_service_hostnames is None:
        allowed_git_service_hostnames = get_allowed_git_service_hostnames(defaults)

    parsed_url = clean_url(url)
    if not parsed_url:
        return None

    res_scheme = ""
    res_path = ""
    res_netloc = ""

    # e.g., https://github.com/owner/project.git
    if parsed_url.scheme in {"http", "https", "ftp", "ftps", "git+https"}:
        if parsed_url.netloc not in allowed_git_service_hostnames:
            return None
        path_params = parsed_url.path.strip("/").split("/")
        if len(path_params) < 2:
            return None

        res_path = "/".join(path_params[:2])
        res_scheme = "https"
        res_netloc = parsed_url.netloc

    # e.g.:
    #   ssh://git@hostname:port/owner/project.git
    #   ssh://git@hostname:owner/project.git
    elif parsed_url.scheme in {"ssh", "git+ssh"}:
        user_host, _, port = parsed_url.netloc.partition(":")
        user, _, host = user_host.rpartition("@")

        if not user or host not in allowed_git_service_hostnames:
            return None

        path = ""
        if not port.isdecimal():
            # Happen for ssh://git@github.com:owner/project.git
            # where parsed_url.netloc="git@github.com:owner", port="owner"
            # and parsed_url.path="project.git".
            # In this case, we merge port with parsed_url.path
            # to get the full path.
            path = f"{port}/{parsed_url.path.strip('/')}"
        else:
            path = parsed_url.path

        path_params = path.strip("/").split("/")
        if len(path_params) < 2:
            return None

        res_path = f"{path_params[0]}/{path_params[1]}"
        res_scheme = "https"
        res_netloc = host

    # e.g., git@github.com:owner/project.git
    elif parsed_url.scheme == "":
        user_host, _, port_path = parsed_url.path.partition(":")
        if not user_host or not port_path:
            return None
        user, _, host = user_host.rpartition("@")
        if not user or host not in allowed_git_service_hostnames:
            return None

        path = ""
        port_num, _, path_remain = port_path.strip("/").partition("/")
        if not port_num.isdecimal():
            # port_path doesn't have any port number (e.g. port_path == /org/name).
            # We use all of port_path as the path.
            path = port_path
        else:
            # port_path have valid port number (e.g. port_path == 7999/org/name).
            # We only use the rest of the path.
            path = path_remain

        path_params = path.strip("/").split("/")
        if len(path_params) < 2:
            return None

        res_path = f"{path_params[0]}/{path_params[1]}"
        res_scheme = "https"
        res_netloc = host

    try:
        return urllib.parse.ParseResult(
            scheme=res_scheme,
            netloc=res_netloc,
            path=res_path,
            params="",
            query="",
            fragment="",
        )
    except ValueError:
        logger.debug("Could not reconstruct %s.", url)
        return None


def get_allowed_git_service_hostnames(config: ConfigParser) -> list[str]:
    """Load allowed git service hostnames from ini configuration.

    Some notes for future improvements:

    The fact that this method is here is not ideal.

    Q: Why do we need this method here in this ``git_url`` module in the first place?
    A: A number of functions in this module also do "URL validation" as part of their logic.
    This requires loading in the allowed git service hostnames from the ini config.

    Q: Why don't we use the ``GIT_SERVICES`` list from the ``macaron.slsa_analyzer.git_service``
    instead of having this second place of loading git service configuration?
    A: Referencing ``GIT_SERVICES`` in this module results in cyclic imports since the module
    where ``GIT_SERVICES`` is defined in also reference this module.
    """
    git_service_section_names = [
        section_name for section_name in config.sections() if section_name.startswith("git_service")
    ]

    allowed_git_service_hostnames = []

    for section_name in git_service_section_names:
        git_service_section = config[section_name]

        hostname = git_service_section.get("hostname")
        if not hostname:
            continue

        allowed_git_service_hostnames.append(hostname)

    return allowed_git_service_hostnames


def get_repo_dir_name(url: str, sanitize: bool = True) -> str:
    """Return the repo directory name from a remote repo url.

    The directory name will be in the form ``<git_host>/org/name``.
    When sanitize is True (default), this method
    makes sure that ``git_host`` is a valid directory name:
    - Contains only lowercase letters and numbers
    - Only starts with lowercase letters or numbers
    - Words are separated by ``_``

    Parameters
    ----------
    url: str
        The remote url of the target repository.
    sanitize: bool
        Sanitizes the name to be a valid directory name (Default True)

    Returns
    -------
    str
        The unique path resolved from the remote path or an empty string if errors.

    Examples
    --------
    >>> get_repo_dir_name("https://github.com/apache/maven")
    'github_com/apache/maven'
    """
    remote_url = get_remote_vcs_url(url)
    if not remote_url:
        logger.debug("URL '%s' is not valid.", url)
        return ""

    parsed_url = parse_remote_url(remote_url)
    if not parsed_url:
        # Shouldn't happen.
        logger.critical("URL '%s' is not valid even though it has been validated.", url)
        return ""

    git_host = parsed_url.netloc

    if not sanitize:
        return os.path.join(git_host, parsed_url.path.strip("/"))

    # Sanitize the path and make sure it's a valid directory name.
    allowed_chars = string.ascii_lowercase + string.digits
    for letter in git_host:
        if letter not in allowed_chars:
            git_host = git_host.replace(letter, "_", 1)

    # Cannot start with _.
    if git_host.startswith("_"):
        git_host = f"mcn{git_host}"

    return os.path.join(git_host, parsed_url.path.strip("/"))


def is_empty_repo(git_obj: Git) -> bool:
    """Return True if the repo has no commit checked out.

    Parameters
    ----------
    git_obj : Git
        The pydriller.Git object of the repository.

    Returns
    -------
    bool
        True if the repo has no commit else False.
    """
    # https://stackoverflow.com/questions/5491832/how-can-i-check-whether-a-git-repository-has-any-commits-in-it
    try:
        head_commit_hash = git_obj.repo.git.rev_parse("HEAD")
        if not head_commit_hash:
            return True

        return False
    except GitCommandError:
        return True


def is_commit_hash(value: str) -> bool:
    """Check if a given string is a valid Git commit hash.

    A valid Git commit hash is a 40-character long hexadecimal string or
    a short version that is at least 7 characters long. The function uses
    a regular expression to match these patterns.

    Parameters
    ----------
    value: str
        The string value to be checked for validity as a commit hash.

    Returns
    -------
    bool: True if the string matches the format of a Git commit hash (7 to 40
        characters long and only contains hexadecimal characters), False otherwise.

    Example
    -------
    >>> is_commit_hash('e3a1b6c')
    True
    >>> is_commit_hash('e3a1b6c8d9b2ff0c9f5f8a0a5d8f4cf2e19b1db3')
    True
    >>> is_commit_hash('invalid_hash123')
    False
    >>> is_commit_hash('master')
    False
    >>> is_commit_hash('main')
    False
    """
    pattern = r"^[a-f0-9]{7,40}$"
    return bool(re.match(pattern, value))


def get_tags_via_git_remote(repo: str) -> dict[str, str] | None:
    """Retrieve all tags from a given repository using ls-remote.

    Parameters
    ----------
    repo: str
        The repository to perform the operation on.

    Returns
    -------
    dict[str]
        A dictionary of tags mapped to their commits, or None if the operation failed..
    """
    tag_data = list_remote_references(["--tags"], repo)
    if not tag_data:
        return None
    tags = {}

    for tag_line in tag_data.splitlines():
        tag_line = tag_line.strip()
        if not tag_line:
            continue
        split = tag_line.split("\t")
        if len(split) != 2:
            continue
        possible_tag = split[1]
        if possible_tag.endswith("^{}"):
            possible_tag = possible_tag[:-3]
        elif possible_tag in tags:
            # If a tag already exists, it must be the annotated reference of an annotated tag.
            # In that case we skip the tag as it does not point to the proper source commit.
            # Note that this should only happen if the tags are received out of standard order.
            continue
        possible_tag = possible_tag.replace("refs/tags/", "")
        if not possible_tag:
            continue
        tags[possible_tag] = split[0]

    logger.debug("Found %s tags via ls-remote of %s", len(tags), repo)

    return tags


def find_highest_git_tag(tags: set[str]) -> str:
    """
    Find and return the highest (most recent) semantic version tag from a set of Git tags.

    Parameters
    ----------
    tags : set[str]
        A set of version strings (e.g., {"v1.0.0", "v2.3.4", "v2.1.0"}). Tags must follow PEP 440 or
        semver format, otherwise they will be skipped.

    Returns
    -------
    str
        The tag string corresponding to the highest version.

    Raises
    ------
    GitTagError
        If no valid tag is found or if a tag is not a valid version.

    Example
    -------
    >>> find_highest_git_tag({"v2.0.0"})
    'v2.0.0'

    >>> find_highest_git_tag({"v4", "v4.2.1"})
    'v4.2.1'

    >>> find_highest_git_tag({"1.2.3", "2.0.0", "1.10.1"})
    '2.0.0'

    >>> find_highest_git_tag({"0.1", "0.1.1", "0.0.9"})
    '0.1.1'

    >>> find_highest_git_tag({"invalid", "1.0.0"})
    '1.0.0'

    >>> find_highest_git_tag(set())
    Traceback (most recent call last):
        ...
    GitTagError: No tags provided.

    >>> find_highest_git_tag({"invalid"})
    Traceback (most recent call last):
        ...
    GitTagError: No valid version tag found.
    """
    if not tags:
        raise GitTagError("No tags provided.")

    highest_tag = None
    highest_parsed_tag = version.Version("0")

    for tag in tags:
        try:
            parsed_tag = version.Version(tag)
        except version.InvalidVersion:
            logger.debug("Invalid version tag encountered while finding the highest tag: %s", tag)
            continue

        if parsed_tag > highest_parsed_tag:
            highest_parsed_tag = parsed_tag
            highest_tag = tag

    if highest_tag is None:
        raise GitTagError("No valid version tag found.")

    return highest_tag
