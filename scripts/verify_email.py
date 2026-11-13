"""真实邮件发送校验：用配置好的 SMTP 账号把会议提醒真实发到指定邮箱。

这是"邮件是否真的发出去了"的唯一可信验证方式——不 mock、不假装。

用法::

    # 1) 在同一个终端里配置 SMTP（推荐用邮箱的授权码/应用专用密码）
    $env:AIEB_SMTP_HOST="smtp.example.com"
    $env:AIEB_SMTP_PORT="465"
    $env:AIEB_SMTP_SECURITY="ssl"
    $env:AIEB_SMTP_USER="you@example.com"
    $env:AIEB_SMTP_PASSWORD="<授权码>"
    $env:AIEB_SMTP_FROM="you@example.com"

    # 2) 真实发送（会写入 emails 表并打印真实结果）
    python scripts/verify_email.py --to you@example.com

    # 只看配置与草稿，不发送
    python scripts/verify_email.py --to you@example.com --dry-run

退出码：0 = 真实发送成功；1 = 发送失败或未配置（会打印原因）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aieb.brain import mailer  # noqa: E402
from aieb.brain.service import BrainService  # noqa: E402
from aieb.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True, help="真实收件地址（建议用你自己的测试邮箱）")
    parser.add_argument("--meeting", default="", help="会议 ID（默认取最近一次会议）")
    parser.add_argument("--dry-run", action="store_true", help="只生成草稿，不发送")
    parser.add_argument("--extra", default="", help="对邮件内容的额外要求")
    args = parser.parse_args()

    cfg = mailer.load_config()
    print("SMTP 配置：", cfg.public_view())
    if mailer.dry_run_enabled():
        print("\n当前是发送保护模式（AIEB_EMAIL_DRY_RUN=1），本脚本不会真实投递。")
        print("如需真实发送，请先取消该环境变量后重跑。")
        return 3
    if not cfg.configured:
        print("\n未配置 SMTP，缺少环境变量：")
        for name in cfg.missing:
            print("  -", name)
        print("\n请在**当前终端**设置这些变量后重跑本脚本（密码建议用授权码，不要用登录密码）。")
        return 1

    config = load_config()
    if not config.providers:
        print("配置文件里没有可用的 provider")
        return 1

    service = BrainService(work_dir=ROOT, provider=config.providers[0])
    try:
        if args.meeting:
            meeting = service.store.get_meeting(args.meeting)
        else:
            meetings = service.store.list_meetings(limit=1)
            meeting = service.store.get_meeting(meetings[0]["id"]) if meetings else None
        if meeting is None:
            print("数据库里还没有会议记录，请先在「会议中心」分析一份会议 TXT。")
            return 1

        print(f"\n会议：{meeting['title']}（风险 {len(meeting.get('risks') or [])} 条 / "
              f"待办 {len(meeting.get('todos') or [])} 条）")
        draft = asyncio.run(service.draft_email(args.meeting or meeting["id"],
                                                to_addr=args.to, extra=args.extra))
        print(f"草稿已生成：{draft['subject']}（id={draft['id']}，状态={draft['status']}）")
        print("--- 正文预览 ---")
        print(draft["body"][:800])
        print("----------------\n")

        if args.dry_run:
            print("--dry-run：未发送，退出。")
            return 0

        result = service.send_email(draft["id"])
        print("发送结果：", result["message"])
        stored = service.store.get_email(draft["id"]) or {}
        print(f"数据库记录状态：{stored.get('status')} / sent_at={stored.get('sent_at') or '—'}")
        if not result.get("ok"):
            print(f"失败原因：{result.get('message')}")
            return 1
        print("\n真实发送成功。请到收件箱确认能否收到（注意查垃圾邮件）。")
        return 0
    finally:
        service.store.close()


if __name__ == "__main__":
    sys.exit(main())
