#!/usr/bin/env python3
"""管理者確認開通 ticket 後，在本機主機建立老師帳號。"""
from pathlib import Path
import argparse
import getpass
from service import Service, APIError

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database",type=Path,default=ROOT/"LocalOnly/server.sqlite")
parser.add_argument("--username",required=True)
parser.add_argument("--alias",required=True,help="顯示代稱，不必使用真實姓名")
args=parser.parse_args()
password=getpass.getpass("設定老師帳號密碼（不顯示，勿貼入公開 ticket）：")
confirmation=getpass.getpass("再次輸入密碼：")
if password!=confirmation: raise SystemExit("兩次密碼不一致，未建立帳號。")
try:
    service=Service(args.database)
    result=service.provision_teacher({"username":args.username,"alias":args.alias,"password":password})
    service.logout(result["token"])
    print("老師帳號已開通。請透過既有可信私人管道交付登入資料；不要放入 GitHub Issues。")
except APIError as error: raise SystemExit(error.message) from None
