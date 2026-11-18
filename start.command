#!/bin/zsh
cd -- "${0:A:h}" || exit 1
PYTHONDONTWRITEBYTECODE=1 python3 closeout.py web
echo
read -r '?按回车关闭…'
