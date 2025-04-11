#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import base64
import json
from pathlib import Path
from itertools import chain
import argparse
from dataclasses import dataclass
from mimetypes import guess_type
from typing import Sequence, List

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# =====================
# Data Classes & Types
# =====================

FileId = str

@dataclass(order=True)
class FileInfo:
    path: Path
    id: str
    parent: DirInfo

@dataclass
class DirInfo:
    path: Path
    id: FileId
    parent: DirInfo | None

@dataclass(order=True)
class UploadTarget:
    path: Path
    folder: DirInfo

@dataclass(order=True)
class UploadInfo:
    target: UploadTarget
    existing_info: FileInfo | None

@dataclass
class FolderTree:
    dir: DirInfo | None
    children: dict[str, FolderTree]

# =====================
# Constants & Settings
# =====================

SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
]

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
RETRIES = 5

# =====================
# Drive Service Class
# =====================

class DriveService:
    def __init__(self, credentials_json: dict):
        self.service_account_mail = credentials_json["client_email"]
        credentials = service_account.Credentials.from_service_account_info(
            credentials_json, scopes=SCOPES
        )
        self.service = build("drive", "v3", credentials=credentials)

    def list_in_folder(self, folder: DirInfo, query: str) -> list[FileInfo]:
        results = (
            self.service.files()
            .list(
                q=f"""
                    '{folder.id}' in parents
                    {'and' if query else ''}
                    {query}
                    """,
                fields="files(id, name)",
            )
            .execute(num_retries=RETRIES)
        )
        return [
            FileInfo(folder.path / f["name"], f["id"], folder)
            for f in results.get("files", [])
        ]

    def list_files_in_folder(self, folder: DirInfo) -> list[FileInfo]:
        return self.list_in_folder(
            folder=folder,
            query=f"'{self.service_account_mail}' in owners and mimeType != '{FOLDER_MIME_TYPE}'",
        )

    def is_owned_by_service(self, fileOrFolder: FileInfo | DirInfo) -> bool:
        results = (
            self.service.files()
            .get(fileId=fileOrFolder.id, fields="owners")
            .execute(num_retries=RETRIES)
        )
        owners = results.get("owners", [])
        return len(owners) == 1 and owners[0]["emailAddress"] == self.service_account_mail

    def list_folders_in_folder(self, folder: DirInfo) -> list[DirInfo]:
        return [
            DirInfo(f.path, f.id, f.parent)
            for f in self.list_in_folder(
                folder=folder,
                query=f"'{self.service_account_mail}' in owners and mimeType = '{FOLDER_MIME_TYPE}'",
            )
        ]

    def is_folder_empty(self, folder: DirInfo) -> bool:
        entries = self.list_in_folder(folder=folder, query="")
        return len(entries) == 0

    def fetch_remote_folder_tree(self, folder: DirInfo) -> FolderTree:
        def build_tree(folder: DirInfo, current_node: FolderTree) -> None:
            folders = self.list_folders_in_folder(folder)
            for folder in folders:
                last = folder.path.parts[-1]
                if last not in current_node.children:
                    node = FolderTree(dir=folder, children={})
                    current_node.children[last] = node
                    build_tree(folder, node)

        root = FolderTree(dir=folder, children={})
        build_tree(folder, root)
        return root

    def ensure_path(self, path: Path, base: DirInfo) -> DirInfo:
        """
        Ensure a given path relative to the base exists on Drive. Returns a DirInfo
        that represents the folder on Drive.
        """
        current = base
        # If path is empty (uploading to base folder), return base
        if not path.parts:
            return current
        for part in path.parts:
            results = (
                self.service.files()
                .list(
                    q=f"'{current.id}' in parents and name = '{part}' and mimeType = '{FOLDER_MIME_TYPE}'",
                    fields="files(id)",
                )
                .execute(num_retries=RETRIES)
            )
            folders = results.get("files", [])
            if folders:
                current = DirInfo(current.path / part, folders[0]["id"], current)
            else:
                # Create folder
                print(
                    f"Folder {part} of path {path} does not exist in drive. Creating it."
                )
                folder = (
                    self.service.files()
                    .create(
                        body={
                            "name": part,
                            "mimeType": FOLDER_MIME_TYPE,
                            "parents": [current.id],
                        },
                        fields="id",
                    )
                    .execute(num_retries=RETRIES)
                )
                fid = folder.get("id")
                if not fid:
                    raise Exception("Could not create folder")
                current = DirInfo(current.path / part, fid, current)
        return current

    def delete(self, file: FileInfo | DirInfo) -> None:
        self.service.files().delete(fileId=file.id).execute(num_retries=RETRIES)

    def batch_delete(self, files: Sequence[FileInfo | DirInfo]) -> None:
        if not files:
            return

        def callback(_requ, _resp, exception):
            if exception:
                print(f"An error occurred: {exception}")

        batch = self.service.new_batch_http_request(callback=callback)
        for file in files:
            print(f"Deleting stale file {file.path} ({file.id})")
            batch.add(self.service.files().delete(fileId=file.id))
        batch.execute()
        print(f"    ==> Done deleting {len(files)} files.")

    def upload_file(self, input_base: Path, upload_info: UploadInfo) -> None:
        file = upload_info.target.path
        folder = upload_info.target.folder
        info = upload_info.existing_info
        mime, _ = guess_type(file)
        if mime is None:
            mime = "*/*"
        media = MediaFileUpload(
            input_base / file, chunksize=1024 * 1024, mimetype=mime, resumable=True
        )
        if info is None:
            print(f"Uploading new file {file.name} to folder {folder.path}")
            request = self.service.files().create(
                body={
                    "name": file.name,
                    "parents": [folder.id],
                },
                media_body=media,
                fields="id",
            )
        else:
            print(f"Updating existing file {file.name}")
            request = self.service.files().update(
                fileId=info.id,
                media_body=media,
            )
        response = None
        while response is None:
            status, response = request.next_chunk(num_retries=RETRIES)
            if status:
                print("...Uploaded %d%%." % int(status.progress() * 100))
        print(f"    ==> Upload of {file.name} complete.")

# =====================
# Utility Functions
# =====================

def decode_credentials(credentials_base64: str) -> dict:
    return json.loads(base64.b64decode(credentials_base64).decode("utf-8"))

def load_credentials(credentials_arg: str) -> dict:
    if credentials_arg.endswith(".json") and Path(credentials_arg).exists():
        with open(credentials_arg, encoding="utf-8") as f:
            return json.load(f)
    return decode_credentials(credentials_arg)

def safe_chdir(path: Path | str) -> None:
    try:
        os.chdir(path)
    except Exception as e:
        print(f"::error Failed to change directory to {path}: {e}")
        sys.exit(1)

def get_upload_targets(
    driveService: DriveService,
    input_base: Path,
    globFilter: str,
    output_folder: DirInfo,
    flat_upload: bool,
    skip_patterns: List[str],
) -> list[UploadTarget]:
    """
    Scans an input folder recursively using the given glob filter. Files matching
    any of the skip_patterns are ignored.
    
    If flat_upload is True, the entire structure is flattened, and the target folder
    is always the provided output folder.
    Otherwise, the relative folder structure from the input folder is preserved.
    """
    safe_chdir(input_base)
    all_files = sorted(f for f in Path("./").rglob(globFilter) if f.is_file())
    safe_chdir(os.getcwd())  # Switch back to previous working directory

    # Apply skip patterns (if any)
    def should_skip(f: Path) -> bool:
        return any(f.match(pattern.strip()) for pattern in skip_patterns)

    targets: list[UploadTarget] = []
    for f in all_files:
        if should_skip(f):
            print(f"Skipping file {f} due to skip pattern")
            continue

        # Compute the relative path from the input_base for structure preservation
        rel_path = f.relative_to(input_base)
        if flat_upload:
            # All files go directly into the output_folder
            target_folder = output_folder
            target_path = f.name  # only file name
        else:
            target_folder = driveService.ensure_path(rel_path.parent, base=output_folder)
            target_path = rel_path

        targets.append(UploadTarget(path=Path(target_path), folder=target_folder))
    return targets

def tree_to_list(tree: FolderTree) -> list[DirInfo]:
    result = [tree.dir] if tree.dir else []
    for child in tree.children.values():
        if child.dir:
            result.append(child.dir)
        result.extend(tree_to_list(child))
    return result

def cleanup_folders(driveService: DriveService, folder: FolderTree) -> None:
    def do_clean(folder: FolderTree) -> None:
        for child in folder.children.values():
            do_clean(child)
        if folder.dir and driveService.is_folder_empty(folder.dir) and driveService.is_owned_by_service(folder.dir):
            print(f"Deleting empty folder {folder.dir.path} ({folder.dir.id})")
            driveService.delete(folder.dir)
            print("    ==> Done")
    do_clean(folder)

# =====================
# Main Functionality
# =====================

def main() -> None:
    parser = argparse.ArgumentParser(
        prog=sys.argv[0],
        epilog="This tool uploads files to Google Drive with options for preserving folder structure, processing multiple branches, and skipping files.",
    )
    parser.add_argument(
        "-i",
        "--input",
        help="Paths to one or more folders to be uploaded",
        nargs="+",
        type=str,
        required=True,
    )
    parser.add_argument(
        "-f",
        "--filter",
        help="Glob pattern to filter files in the input folder",
        nargs=1,
        type=str,
        default=["*"],
        required=False,
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Path to the folder in the drive to which the files should be uploaded",
        nargs=1,
        type=str,
        required=True,
    )
    parser.add_argument(
        "-t",
        "--target",
        help="Folder id of the drive root folder",
        nargs=1,
        type=str,
        required=True,
    )
    parser.add_argument(
        "-c",
        "--credentials",
        help="Base64 encoded credentials.json or a path to a credentials file",
        nargs=1,
        type=str,
        required=True,
    )
    parser.add_argument(
        "--purge-stale",
        help="Delete stale files (i.e. files which aren't present locally) in the output folder",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--flat-upload",
        help="If set, upload all files directly into the output folder (flattening folder structure)",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--skip",
        help="Comma-separated glob patterns of files to skip (e.g., '*.tmp,*.log')",
        nargs="?",
        type=str,
        default="",
    )
    args = parser.parse_args()

    print("==== Arguments ====")
    for arg_name, arg_value in vars(args).items():
        print(f"    {arg_name}: {arg_value}")

    credentials_json = load_credentials(args.credentials[0])
    target_id: str = args.target[0]
    # Process multiple input directories
    input_folders = [Path(path) for path in args.input]
    globFilter: str = args.filter[0]
    output_folder_path = Path(args.output[0])
    flat_upload: bool = args.flat_upload
    skip_patterns: List[str] = [p for p in args.skip.split(",") if p.strip()]

    # Ensure all input folders exist
    for input_folder_path in input_folders:
        if not input_folder_path.exists():
            raise FileNotFoundError(f"Input folder {input_folder_path} does not exist")
        if not input_folder_path.is_dir():
            raise NotADirectoryError(f"Input folder {input_folder_path} is not a directory")

    driveService = DriveService(credentials_json)
    base_folder = DirInfo(Path(""), target_id, None)
    # Ensure the output folder exists on Drive
    output_folder = driveService.ensure_path(output_folder_path, base=base_folder)

    # Build upload targets from all input folders
    all_upload_targets: List[UploadTarget] = []
    for input_folder in input_folders:
        print(f"==== Scanning local files in: {input_folder} ====")
        targets = get_upload_targets(
            driveService, input_folder, globFilter, output_folder, flat_upload, skip_patterns
        )
        # When preserving structure, adjust the upload target paths
        # so that each input branch's relative path is preserved
        if not flat_upload:
            # Optionally, you might want to create a branch folder on Drive for each input folder.
            # For now, the relative path (from the input folder root) is preserved.
            pass
        all_upload_targets.extend(targets)

    print("==== Local Files to Upload ====")
    for upload_target in all_upload_targets:
        print(f"{upload_target.path} will be uploaded to folder {upload_target.folder.id} ({upload_target.folder.path})")

    # Combine local folders found while scanning the uploads
    local_folders_to_consider = {t.folder.id: t.folder for t in all_upload_targets}

    # Prevent remote paths from including the path of the output folder.
    remote_base = DirInfo(Path(""), output_folder.id, None)
    remote_folder_tree = driveService.fetch_remote_folder_tree(remote_base)
    remote_folders_to_consider = {t.id: t for t in tree_to_list(remote_folder_tree)}
    folders_to_consider = {**local_folders_to_consider, **remote_folders_to_consider}

    print("==== Considering the following remote folders ====")
    for fid, folder in folders_to_consider.items():
        print(f"{folder.path} ({fid})")

    remote_files = list(
        chain.from_iterable(
            driveService.list_files_in_folder(folder)
            for (_, folder) in folders_to_consider.items()
        )
    )

    print("==== Remote Files ====")
    for f in remote_files:
        print(f"{f.path} ({f.id})")

    print("==== Uploading Files ====")
    remote_files_by_path = {f.path: f for f in remote_files}
    files_to_upload = [
        UploadInfo(
            target=f,
            existing_info=remote_files_by_path.get(f.path, None),
        )
        for f in all_upload_targets
    ]
    # Process uploads for each input branch; note that flat uploads ignore folder structure.
    for upload_info in files_to_upload:
        driveService.upload_file(input_base=output_folder.path if flat_upload else input_folder, upload_info=upload_info)

    if args.purge_stale:
        print("==== Removing Stale Remote Files ====")
        input_paths_set = {f.path for f in all_upload_targets}
        stale_files = [f for f in remote_files if f.path not in input_paths_set]
        driveService.batch_delete(stale_files)

        print("==== Cleaning Up Empty Folders ====")
        cleanup_folders(driveService, remote_folder_tree)

def main_with_github_reporting():
    try:
        main()
    except Exception as e:
        print(f"::error {e}")
        sys.exit(1)

if __name__ == "__main__":
    main_with_github_reporting()
