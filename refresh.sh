#!/bin/zsh
# Refresh listings and publish. Run by launchd (com.sly.carhunt) every 6 hours.
set -e
cd "${0:A:h}"
/opt/homebrew/bin/git pull -q --rebase
/opt/homebrew/bin/python3 -m unittest -q
/opt/homebrew/bin/python3 scrape.py
/opt/homebrew/bin/git add docs/data/listings.json
if ! /opt/homebrew/bin/git diff --cached --quiet; then
  /opt/homebrew/bin/git commit -q -m "data: refresh listings"
  /opt/homebrew/bin/git push -q --no-verify
fi
