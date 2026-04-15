"""
Trello Weekly Report → Slack Block Kit
Claude Code Routine용 스크립트

환경변수 (Routine 클라우드 환경에 설정):
  TRELLO_API_KEY    - Trello API Key
  TRELLO_TOKEN      - Trello Token
  SLACK_WEBHOOK_URL - Slack Incoming Webhook URL
  TRELLO_WORKSPACE  - (선택) 특정 워크스페이스 이름 필터
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ──────────────────────────────────────────
# 설정
# ──────────────────────────────────────────

TRELLO_KEY   = os.environ["TRELLO_API_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
WORKSPACE_FILTER = os.environ.get("TRELLO_WORKSPACE", "")  # 비워두면 전체 보드

DAYS_BACK = 7  # 지난 N일 기준
BASE_URL  = "https://api.trello.com/1"

auth = {"key": TRELLO_KEY, "token": TRELLO_TOKEN}
now  = datetime.now(timezone.utc)
since = now - timedelta(days=DAYS_BACK)
since_str = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")

# ──────────────────────────────────────────
# Trello API 헬퍼
# ──────────────────────────────────────────

def trello_get(path, params=None):
    p = {**auth, **(params or {})}
    r = requests.get(f"{BASE_URL}{path}", params=p, timeout=15)
    r.raise_for_status()
    return r.json()

def get_member_name(member_id, cache={}):
    if member_id not in cache:
        try:
            m = trello_get(f"/members/{member_id}", {"fields": "fullName"})
            cache[member_id] = m.get("fullName", member_id)
        except Exception:
            cache[member_id] = member_id
    return cache[member_id]

# ──────────────────────────────────────────
# 데이터 수집
# ──────────────────────────────────────────

print("📋 Trello 보드 목록 조회 중...")
boards = trello_get("/members/me/boards", {
    "filter": "open",
    "fields": "id,name,url,idOrganization",
})

if WORKSPACE_FILTER:
    orgs = trello_get("/members/me/organizations", {"fields": "id,displayName"})
    target_org_ids = {o["id"] for o in orgs if WORKSPACE_FILTER.lower() in o["displayName"].lower()}
    boards = [b for b in boards if b.get("idOrganization") in target_org_ids]

print(f"✅ {len(boards)}개 보드 발견")

# 보드별 데이터 수집
report = []

for board in boards:
    bid   = board["id"]
    bname = board["name"]
    burl  = board["url"]
    print(f"  → {bname} 처리 중...")

    # 리스트 이름 맵
    lists = trello_get(f"/boards/{bid}/lists", {"fields": "id,name"})
    list_map = {l["id"]: l["name"] for l in lists}

    # 해당 보드의 카드 액션 (지난 7일)
    actions = trello_get(f"/boards/{bid}/actions", {
        "filter": "createCard,updateCard,moveCardToBoard",
        "since": since_str,
        "limit": 1000,
        "fields": "type,date,data,idMemberCreator",
    })

    created_cards   = {}  # id → card info
    completed_cards = {}
    moved_cards     = {}
    member_activity = defaultdict(set)  # member_name → set of card ids

    for action in actions:
        atype = action["type"]
        data  = action["data"]
        adate = action["date"]
        mid   = action.get("idMemberCreator", "")
        card  = data.get("card", {})
        cid   = card.get("id", "")
        cname = card.get("name", "")

        if not cid:
            continue

        member_name = get_member_name(mid) if mid else "Unknown"
        member_activity[member_name].add(cid)

        if atype == "createCard":
            created_cards[cid] = {
                "name": cname,
                "list": list_map.get(data.get("list", {}).get("id", ""), ""),
                "member": member_name,
                "date": adate[:10],
            }

        elif atype == "updateCard":
            # 완료 감지: listAfter 이름에 '완료', 'Done', 'Complete' 포함
            list_after = data.get("listAfter", {})
            list_name  = list_map.get(list_after.get("id", ""), list_after.get("name", ""))
            if any(kw in list_name.lower() for kw in ["완료", "done", "complete", "finished", "closed"]):
                completed_cards[cid] = {
                    "name": cname,
                    "list": list_name,
                    "member": member_name,
                    "date": adate[:10],
                }
            elif list_after:
                moved_cards[cid] = {
                    "name": cname,
                    "from": list_map.get(data.get("listBefore", {}).get("id", ""), ""),
                    "to": list_name,
                    "member": member_name,
                    "date": adate[:10],
                }

    report.append({
        "board_name": bname,
        "board_url": burl,
        "created":   list(created_cards.values()),
        "completed": list(completed_cards.values()),
        "moved":     list(moved_cards.values()),
        "members":   {k: len(v) for k, v in member_activity.items()},
    })

# ──────────────────────────────────────────
# 통계 집계
# ──────────────────────────────────────────

total_created   = sum(len(b["created"])   for b in report)
total_completed = sum(len(b["completed"]) for b in report)
total_moved     = sum(len(b["moved"])     for b in report)

# 전체 담당자별 활동 합산
all_members = defaultdict(int)
for b in report:
    for m, cnt in b["members"].items():
        all_members[m] += cnt

# ──────────────────────────────────────────
# Slack Block Kit 메시지 구성
# ──────────────────────────────────────────

date_range = f"{since.strftime('%Y.%m.%d')} ~ {now.strftime('%Y.%m.%d')}"

blocks = []

# ── 헤더 ──────────────────────────────────
blocks += [
    {
        "type": "header",
        "text": {"type": "plain_text", "text": f"📋 Trello 주간 리포트", "emoji": True}
    },
    {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"*기간:* {date_range}  |  *보드:* {len(report)}개"}]
    },
    {"type": "divider"},
]



# ── 보드별 상세 ────────────────────────────
for b in report:
    if not any([b["created"], b["completed"], b["moved"]]):
        continue  # 활동 없는 보드는 스킵

    board_total = len(b["created"]) + len(b["completed"]) + len(b["moved"])
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*<{b['board_url']}|{b['board_name']}>*  _{board_total}건 활동_"
        }
    })

    details = []

    if b["completed"]:
        lines = "\n".join(f"  · {c['name']} ({c['member']}, {c['date']})" for c in b["completed"][:5])
        suffix = f"\n  _외 {len(b['completed'])-5}건_" if len(b["completed"]) > 5 else ""
        details.append(f"✅ *완료* {len(b['completed'])}건\n{lines}{suffix}")

    if b["created"]:
        lines = "\n".join(f"  · {c['name']} ({c['member']}, {c['date']})" for c in b["created"][:5])
        suffix = f"\n  _외 {len(b['created'])-5}건_" if len(b["created"]) > 5 else ""
        details.append(f"🆕 *신규* {len(b['created'])}건\n{lines}{suffix}")

    if b["moved"]:
        lines = "\n".join(
            f"  · {c['name']}  `{c['from']} → {c['to']}`" for c in b["moved"][:5]
        )
        suffix = f"\n  _외 {len(b['moved'])-5}건_" if len(b["moved"]) > 5 else ""
        details.append(f"🔄 *이동* {len(b['moved'])}건\n{lines}{suffix}")

    if details:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n\n".join(details)}
        })

    blocks.append({"type": "divider"})

# ── 푸터 ──────────────────────────────────
blocks.append({
    "type": "context",
    "elements": [
        {"type": "mrkdwn", "text": f"🤖 Claude Code Routine이 자동 생성  |  {now.strftime('%Y-%m-%d %H:%M')} UTC"}
    ]
})

# ──────────────────────────────────────────
# Slack 전송
# ──────────────────────────────────────────

payload = {
    "text": f"📋 Trello 주간 리포트 ({date_range})",  # 알림 fallback 텍스트
    "blocks": blocks,
}

print("\n📤 Slack으로 전송 중...")
resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=15)
resp.raise_for_status()

print(f"✅ 전송 완료! (보드 {len(report)}개, 완료 {total_completed}건, 신규 {total_created}건)")