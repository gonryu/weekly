#!/usr/bin/env python3
"""
Trello Weekly Report
Collects Trello activity from the last 7 days and sends a report to Slack.
"""

import os
import sys
import time
import json
from datetime import datetime, timezone, timedelta

import requests

# ── Configuration ────────────────────────────────────────────────────────────

TRELLO_API_KEY = os.environ.get("TRELLO_API_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

TRELLO_BASE = "https://api.trello.com/1"
# Rate limit: 300 requests per 10 seconds → ~30 req/s
# We stay well under by throttling to 20 req/s (50 ms between calls)
RATE_LIMIT_DELAY = 0.05   # seconds between requests
REQUEST_TIMEOUT = 30       # seconds per HTTP request
REPORT_DAYS = 7


# ── Trello helpers ────────────────────────────────────────────────────────────

def trello_auth():
    return {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN}


def trello_get(path: str, params: dict = None, retries: int = 3):
    """GET a Trello endpoint with rate-limit throttling and basic retry."""
    url = f"{TRELLO_BASE}{path}"
    p = {**(params or {}), **trello_auth()}
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=p, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:          # Too Many Requests
                wait = 10 + attempt * 5
                print(f"  [rate-limit] waiting {wait}s …", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(RATE_LIMIT_DELAY)
            return resp.json()
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}] {exc} — waiting {wait}s …", flush=True)
            time.sleep(wait)


# ── Data collection ───────────────────────────────────────────────────────────

def get_boards():
    """Return all open boards the token owner can access."""
    print("Fetching boards …", flush=True)
    boards = trello_get("/members/me/boards", {"filter": "open", "fields": "id,name,url"})
    print(f"  Found {len(boards)} open board(s).", flush=True)
    return boards


def get_board_actions(board_id: str, since_iso: str):
    """
    Return all actions on a board since *since_iso*.
    Handles pagination automatically.
    """
    actions = []
    before = None
    page = 0
    while True:
        params = {
            "filter": "all",
            "since": since_iso,
            "limit": 1000,
            "fields": "id,type,date,data,idMemberCreator",
        }
        if before:
            params["before"] = before

        batch = trello_get(f"/boards/{board_id}/actions", params)
        if not batch:
            break
        actions.extend(batch)
        page += 1
        # If we got a full page there might be more
        if len(batch) < 1000:
            break
        before = batch[-1]["id"]

    return actions


def fetch_member_name(member_id: str, cache: dict):
    if member_id in cache:
        return cache[member_id]
    try:
        info = trello_get(f"/members/{member_id}", {"fields": "fullName,username"})
        name = info.get("fullName") or info.get("username", member_id)
    except Exception:
        name = member_id
    cache[member_id] = name
    return name


# ── Summarisation ─────────────────────────────────────────────────────────────

ACTION_LABELS = {
    "createCard":          "카드 생성",
    "updateCard":          "카드 업데이트",
    "deleteCard":          "카드 삭제",
    "commentCard":         "카드 댓글",
    "addMemberToCard":     "카드에 멤버 추가",
    "removeMemberFromCard":"카드에서 멤버 제거",
    "moveCardToBoard":     "카드 보드 이동",
    "moveCardFromBoard":   "카드 보드 이동",
    "createList":          "리스트 생성",
    "updateList":          "리스트 업데이트",
    "archiveList":         "리스트 보관",
    "createBoard":         "보드 생성",
    "updateBoard":         "보드 업데이트",
    "addMemberToBoard":    "보드에 멤버 추가",
    "removeMemberFromBoard":"보드에서 멤버 제거",
    "addChecklistToCard":  "체크리스트 추가",
    "updateCheckItemStateOnCard": "체크리스트 항목 업데이트",
    "createChecklist":     "체크리스트 생성",
    "deleteChecklist":     "체크리스트 삭제",
    "addAttachmentToCard": "첨부 파일 추가",
    "deleteAttachmentFromCard": "첨부 파일 삭제",
}


def summarise_actions(actions: list, member_cache: dict):
    """Return a compact summary dict for a list of board actions."""
    by_type: dict[str, int] = {}
    by_member: dict[str, int] = {}

    for a in actions:
        atype = a.get("type", "unknown")
        by_type[atype] = by_type.get(atype, 0) + 1

        mid = a.get("idMemberCreator")
        if mid:
            name = fetch_member_name(mid, member_cache)
            by_member[name] = by_member.get(name, 0) + 1

    return {"by_type": by_type, "by_member": by_member, "total": len(actions)}


# ── Slack formatting ──────────────────────────────────────────────────────────

def build_slack_message(board_reports: list, since: datetime, until: datetime, total_actions: int):
    since_str = since.strftime("%Y-%m-%d")
    until_str = until.strftime("%Y-%m-%d")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📋 Trello 주간 리포트 ({since_str} ~ {until_str})",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*기간:* {since_str} ~ {until_str}\n"
                    f"*보드 수:* {len(board_reports)}개\n"
                    f"*총 활동:* {total_actions}건"
                ),
            },
        },
        {"type": "divider"},
    ]

    for br in board_reports:
        if br["total"] == 0:
            continue  # skip quiet boards

        # Top action types (up to 5)
        top_types = sorted(br["by_type"].items(), key=lambda x: -x[1])[:5]
        type_lines = "\n".join(
            f"  • {ACTION_LABELS.get(t, t)}: {cnt}건"
            for t, cnt in top_types
        )

        # Top members (up to 5)
        top_members = sorted(br["by_member"].items(), key=lambda x: -x[1])[:5]
        member_lines = "\n".join(
            f"  • {m}: {cnt}건" for m, cnt in top_members
        )

        board_text = f"*<{br['url']}|{br['name']}>* — 총 {br['total']}건\n"
        if type_lines:
            board_text += f"*활동 유형:*\n{type_lines}\n"
        if member_lines:
            board_text += f"*멤버별 활동:*\n{member_lines}"

        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": board_text}}
        )

    if not any(br["total"] > 0 for br in board_reports):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_지난 7일간 활동이 없습니다._",
                },
            }
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Generated by trello_weekly_report.py at {until.strftime('%Y-%m-%d %H:%M')} UTC",
                }
            ],
        }
    )

    return {
        "text": f"Trello 주간 리포트 ({since_str} ~ {until_str}) — 총 {total_actions}건",
        "blocks": blocks,
    }


def send_to_slack(payload: dict):
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp


# ── Main ──────────────────────────────────────────────────────────────────────

def validate_env():
    missing = [
        v for v in ("TRELLO_API_KEY", "TRELLO_TOKEN", "SLACK_WEBHOOK_URL")
        if not os.environ.get(v)
    ]
    if missing:
        print(f"ERROR: 다음 환경변수가 설정되지 않았습니다: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def main():
    validate_env()

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=REPORT_DAYS)
    since_iso = since.isoformat()

    print(f"Trello 주간 리포트 생성 시작")
    print(f"기간: {since.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')} (UTC)\n")

    boards = get_boards()
    if not boards:
        print("접근 가능한 보드가 없습니다.")
        sys.exit(0)

    member_cache: dict = {}
    board_reports = []
    total_actions = 0

    for i, board in enumerate(boards, 1):
        bname = board["name"]
        bid = board["id"]
        burl = board.get("url", "")
        print(f"[{i}/{len(boards)}] '{bname}' 보드 활동 수집 중 …", flush=True)

        try:
            actions = get_board_actions(bid, since_iso)
        except Exception as exc:
            print(f"  경고: '{bname}' 보드 데이터 수집 실패 — {exc}", flush=True)
            actions = []

        summary = summarise_actions(actions, member_cache)
        summary["name"] = bname
        summary["url"] = burl
        board_reports.append(summary)
        total_actions += summary["total"]
        print(f"  → {summary['total']}건 활동 수집 완료", flush=True)

    print(f"\n총 {len(board_reports)}개 보드에서 {total_actions}건 활동 수집 완료.")
    print("Slack 메시지 전송 중 …", flush=True)

    payload = build_slack_message(board_reports, since, now, total_actions)
    send_to_slack(payload)

    print("Slack 전송 완료!")
    print(f"\n요약: {len(board_reports)}개 보드, {total_actions}건 활동 리포팅")


if __name__ == "__main__":
    main()
