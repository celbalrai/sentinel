#!/bin/bash
set -evx

mkdir ~/.bytzcoin

# safety check
if [ ! -f ~/.bytzcoin/bytz.conf ]; then
  cp share/bytz.conf.example ~/.bytzcoin/bytz.conf
fi
