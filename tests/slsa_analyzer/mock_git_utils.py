# Copyright (c) 2022 - 2025, Oracle and/or its affiliates. All rights reserved.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/.

"""
This module contains the methods for preparing mock git repositories for testing SLSA checks.
"""

import os

import git
from git.exc import GitError
from pydriller.git import Git

from macaron.database.table_definitions import Analysis, Component, RepoFinderMetadata, Repository
from macaron.slsa_analyzer.analyze_context import AnalyzeContext


def initiate_repo(repo_path: str | os.PathLike, git_init_options: dict | None = None) -> Git:
    """Init the repo at `repo_path` and return a Git wrapper of that repository.

    This function will create the directory `repo_path` if it does not exist.

    Parameters
    ----------
    repo_path : str | os.PathLike
        The path to the target repo.

    git_init_options : dict
        Additional keyword arguments passed to the `git.Repo.init` method.
        Each key is the name of the argument and each value is the corresponding value
        for the argument.

    Returns
    -------
    Git
        The wrapper of the Git repository.
    """
    git_init_options = git_init_options or {}

    if not os.path.isdir(repo_path):
        os.makedirs(repo_path)

    try:
        git_wrapper = Git(repo_path)
        return git_wrapper
    except GitError:
        # No git repo at repo_path.
        git.Repo.init(repo_path, **git_init_options)
        return Git(repo_path)


def commit_files(git_wrapper: Git, file_names: list) -> bool:
    """Commit the files to the repository indicated by the git_wrapper.

    Parameters
    ----------
    git_wrapper : Git
        The git wrapper.
    file_names : list
        The list of file names in the repository to commit.

    Returns
    -------
    bool
        True if succeed else False.
    """
    try:
        # Store the index object as recommended by the documentation.
        current_index = git_wrapper.repo.index
        current_index.add(file_names)
        current_index.commit(f"Add files: {str(file_names)}")
        return True
    except GitError:
        return False


def prepare_repo_for_testing(
    repo_path: str | os.PathLike, macaron_path: str | os.PathLike, output_dir: str | os.PathLike
) -> AnalyzeContext:
    """Create and commit files for a repo.

    Parameters
    ----------
    repo_path : str or os.PathLike
        The path to the repo.
    macaron_path : str or os.PathLike
        Macaron root path.
    output_dir : str or os.PathLike
        The output dir for creating the AnalyzeContext instance.

    Returns
    -------
    AnalyzeContext
        The AnalyzeContext instance of this target repo.
    """
    git_repo = initiate_repo(repo_path)

    # Commit untracked files
    if git_repo.repo.untracked_files:
        commit_files(git_repo, git_repo.repo.untracked_files)

    component = Component(
        purl="pkg:github/package-url/purl-spec@244fd47e07d1004f0aed9c",
        analysis=Analysis(),
        repository=Repository(
            complete_name="github.com/package-url/purl-spec",
            remote_path=str(repo_path),
            branch_name="master",
            commit_sha="",
            commit_date="",
            files=git_repo.files(),
            fs_path=str(repo_path),
        ),
        repo_finder_metadata=RepoFinderMetadata(),
    )

    analyze_ctx = AnalyzeContext(component=component, macaron_path=str(macaron_path), output_dir=str(output_dir))

    return analyze_ctx
