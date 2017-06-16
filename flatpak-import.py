#!/usr/bin/env python2

from argparse import ArgumentParser
from ConfigParser import ConfigParser
import logging
import os
from sys import exit

import gi
gi.require_version('OSTree', '1.0')
from gi.repository import GLib, Gio, OSTree


OSTREE_COMMIT_GVARIANT_STRING = "(a{sv}aya(say)sstayay)"

OSTREE_STATIC_DELTA_META_ENTRY_FORMAT = "(uayttay)"

OSTREE_STATIC_DELTA_FALLBACK_FORMAT = "(yaytt)"

OSTREE_STATIC_DELTA_SUPERBLOCK_FORMAT = \
    "(a{sv}tayay" + OSTREE_COMMIT_GVARIANT_STRING + \
    "aya" + OSTREE_STATIC_DELTA_META_ENTRY_FORMAT + \
    "a" + OSTREE_STATIC_DELTA_FALLBACK_FORMAT + \
    ")"


if __name__ == "__main__":
    aparser = ArgumentParser(
        description='Import flatpak into a local repository ',
    )
    aparser.add_argument('repo',
                         help='repository name to use')
    aparser.add_argument('flatpak', help='file to import')
    aparser.add_argument('-g', '--gpg-homedir',
                         help='GPG homedir to use when looking for keyrings')
    aparser.add_argument('-k', '--keyring', help='additional trusted keyring file')
    aparser.add_argument('-s', '--sign-key', help='GPG key ID to sign the commit with')
    aparser.add_argument('-v', '--verbose', action='store_true',
                         help='verbose log output')
    aparser.add_argument('-d', '--debug', action='store_true',
                         help='debug log output')

    config = ConfigParser()
    config.read('flatpak-import.conf')
    aparser.set_defaults(**dict(config.items('defaults')))

    args = aparser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    elif args.verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)

    ### Mmap the flatpak file and create a GLib.Variant from it
    mapped_file = GLib.MappedFile.new(args.flatpak, False)
    mapped_bytes = mapped_file.get_bytes()
    ostree_static_delta_superblock_format = GLib.VariantType(
            OSTREE_STATIC_DELTA_SUPERBLOCK_FORMAT)
    delta = GLib.Variant.new_from_bytes(
            ostree_static_delta_superblock_format,
            mapped_bytes,
            False)

    ### Parse flatpak metadata
    # Use get_child_value instead of array index to avoid
    # slowdown (constructing the whole array?)
    checksum_variant = delta.get_child_value(3)
    if not OSTree.validate_structureof_csum_v(checksum_variant):
        # Checksum format invalid
        exit(1)

    metadata_variant = delta.get_child_value(0)
    logging.debug("metadata keys: {0}".format(metadata_variant.keys()))

    commit = OSTree.checksum_from_bytes_v(checksum_variant)

    metadata = {}
    metadata_keys = [ 'ref',
            'flatpak',
            'origin',
            'runtime-repo',
            'metadata',
            'gpg-keys' ]

    for key in metadata_keys:
        try:
            metadata[key] = metadata_variant[key]
        except KeyError:
            metadata[key] = ''
        logging.debug("{0}: {1}".format(key, metadata[key]))

    # Open repository
    repo_file = Gio.File.new_for_path(args.repo)
    repo = OSTree.Repo(path=repo_file)
    if os.path.exists(os.path.join(args.repo, 'config')):
        logging.info('Opening repo at ' + args.repo)
        repo.open()
    else:
        logging.info('Creating archive-z2 repo at ' + args.repo)
        os.makedirs(args.repo)
        repo.create(OSTree.RepoMode.ARCHIVE_Z2)

    # Prepare transaction
    repo.prepare_transaction(None)
    repo.transaction_set_ref(None, metadata['ref'], commit)

    # Execute the delta
    flatpak_file = Gio.File.new_for_path(args.flatpak)
    repo.static_delta_execute_offline(flatpak_file, False, None)

    # Verify gpg signature
    if args.gpg_homedir:
        gpg_homedir = Gio.File.new_for_path(args.gpg_homedir)
    else:
        gpg_homedir = None
    if args.keyring:
        trusted_keyring = Gio.File.new_for_path(args.keyring)
    else:
        trusted_keyring = None
    gpg_verify_result = repo.verify_commit_ext(commit,
                                               keyringdir=gpg_homedir,
                                               extra_keyring=trusted_keyring,
                                               cancellable=None)
    # TODO: Handle signature requirements
    if gpg_verify_result and gpg_verify_result.count_valid() > 0:
        logging.info("valid signature found")
    else:
        logging.error("no valid signature")
        exit(1)

    # Commit the transaction
    repo.commit_transaction(None)

    # Grab commit root
    ret, commit_root, commit_checksum = repo.read_commit(metadata['ref'], None)
    if not ret:
        logging.critical("commit failed")
        exit(1)
    else:
        logging.debug("commit_checksum: " + commit_checksum)

    # Compare installed and header metadata, remove commit if mismatch
    metadata_file = Gio.File.resolve_relative_path (commit_root, "metadata");
    # TODO: GLib-GIO-CRITICAL **: g_file_input_stream_query_info: assertion
    # 'G_IS_FILE_INPUT_STREAM (stream)' failed
    ret, metadata_contents, _ = metadata_file.load_contents(cancellable=None)
    if ret:
        if metadata_contents == metadata['metadata']:
            logging.debug("committed metadata matches the static delta header")
        else:
            logging.critical("committed metadata does not match the static delta header")
            repo.set_ref_immediate(remote=None,
                                   ref=metadata['ref'],
                                   checksum=None,
                                   cancellable=None)
            exit(1)
    else:
        logging.critical("no metadata found in commit")
        exit(1)

    # Sign the commit
    if args.sign_key:
        logging.debug("should sign with key " + args.sign_key)
        ret = repo.sign_commit(commit_checksum=commit,
                               key_id=args.sign_key,
                               homedir=args.gpg_homedir,
                               cancellable=None)
        logging.debug("sign_commit returned " + str(ret))
