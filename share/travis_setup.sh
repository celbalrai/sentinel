#!/bin/bash
set -evx

mkdir ~/.bytz

# safety check
if [ ! -f ~/.bytz/.bytz.conf ]; then
  cp share/bytz.conf.example ~/.bytz/bytz.conf
fi
