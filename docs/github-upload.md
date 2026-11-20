# Publishing Orkestryn 2.0

The independent destination is `williamf48/Orkestryn-2.0`. The original `williamf48/Orkestryn` repository carries the previous project and is not the destination for these files.

The source tree includes the main app, judge demo, research studies and the verified portable build under `downloads/`. Its checksum is recorded next to it. Check publication contents with `python scripts/check_repository.py --staged` after staging; generate fresh distribution files with `python scripts/build_release.py --output dist`. Local databases, generated accounts, private volunteer responses and development logs do not belong in the repository.

For a release attributed to the maintainer’s own GitHub account, publish the tag and attachments from that account. GitHub Actions can run the offline test suite independently. The optional manual release workflow publishes as a workflow identity if explicitly dispatched.
