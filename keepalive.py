#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天翼云电脑 Web 客户端保活脚本（在 GitHub Actions 的 Ubuntu 运行器上执行）

流程：登录 pc.ctyun.cn → 【先检测云电脑是否已有连接】
     → 若已有连接（用户正在使用）：本轮直接跳过，不触发连接，
       等下一次定时触发再判断（避免把正在工作的用户踢下线）；
     → 若无人连接：点击“进入AI云电脑” → 等待真正建立连接
       （若云电脑处于休眠，连接请求会触发平台自动唤醒，需 2~3 分钟）→
       保持连接约 90 秒，期间每 10 秒移动一次鼠标（产生协议层输入）→ 退出。
目的：重置天翼云公众版“连续 1 小时无连接自动休眠”计时器。

凭证通过环境变量注入（GitHub Secrets，代码中不出现明文）：
     CTYUN_USERNAME  天翼云账号（手机号）
     CTYUN_PASSWORD  登录密码
可选：HOLD_SECONDS  保持连接的秒数（默认 90）
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright

USERNAME = os.environ.get("CTYUN_USERNAME", "")
PASSWORD = os.environ.get("CTYUN_PASSWORD", "")
HOLD_SECONDS = int(os.environ.get("HOLD_SECONDS", "90"))

LAUNCH_ARGS = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]

# 桌面列表页上表明“云电脑已被连接/正在被使用”的状态关键词。
# 【校准提示】这些文案基于合理推测；首次部署后请在“自己连接着云电脑”的
# 时段观察一轮 Actions 日志里的“云电脑当前状态：xxx”，若实际文案不同
# （例如显示其他字样），把实际文案补进这个列表即可。
IN_USE_KEYWORDS = ["已连接", "连接中", "使用中", "占用中", "正在使用"]

# 已知的“无人连接”状态（开机/休眠/关机都可以安全触发保活连接）
IDLE_STATES = ["运行中", "休眠中", "已休眠", "休眠", "已关机", "关机"]


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def click_visible_login(page) -> None:
    """页面上有多个含“登录”字样的按钮，只点账号登录区那个可见的。"""
    buttons = page.locator('button:has-text("登录")')
    for i in range(buttons.count()):
        btn = buttons.nth(i)
        if btn.is_visible():
            btn.click()
            return
    raise RuntimeError("未找到可见的登录按钮")


def read_body_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def detect_desktop_state(page) -> str:
    """
    读取桌面列表页文本，判断云电脑当前状态。

    返回值：
      "IN_USE"        —— 检测到占用关键词，云电脑已有连接
      "IN_USE:<关键词>" —— 命中的具体关键词，便于日志校准
      其他字符串      —— 识别到的空闲状态（运行中/休眠中/…）
      "UNKNOWN"       —— 未识别到任何已知状态（默认按空闲处理，继续保活）
    """
    body = read_body_text(page)
    if not body:
        return "UNKNOWN"
    for kw in IN_USE_KEYWORDS:
        if kw in body:
            return f"IN_USE:{kw}"
    for st in IDLE_STATES:
        if st in body:
            return st
    return "UNKNOWN"


def connecting_indicator_visible(page) -> bool:
    """页面是否还显示“正在连接”类提示（连接尚未真正建立）。"""
    for text in ("正在为您连接", "加密通道", "正在为您重连"):
        loc = page.get_by_text(text)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            pass
    return False


def wait_connected(page, timeout: int = 150) -> bool:
    """等待“正在连接”提示消失；云电脑休眠唤醒可能需要 2~3 分钟。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not connecting_indicator_visible(page):
            return True
        time.sleep(5)
    return False


def main() -> int:
    if not USERNAME or not PASSWORD:
        log("FATAL: 未设置 CTYUN_USERNAME / CTYUN_PASSWORD 环境变量（请检查仓库 Secrets）")
        return 1

    with sync_playwright() as p:
        # 优先使用 GitHub Ubuntu 运行器预装的 Google Chrome，免去下载浏览器
        try:
            browser = p.chromium.launch(channel="chrome", headless=True, args=LAUNCH_ARGS)
        except Exception:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)

        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(30000)

        try:
            log("1. 打开登录页")
            page.goto("https://pc.ctyun.cn/#/login", wait_until="domcontentloaded")

            log("2. 填写账号密码")
            page.fill('input[placeholder="请输入手机号/邮箱/账号"]', USERNAME)
            page.fill('input[placeholder="请输入密码"]', PASSWORD)

            log("3. 点击登录")
            click_visible_login(page)

            log("4. 等待登录完成（跳转桌面列表）")
            page.wait_for_url("**/desktop-list**", timeout=45000)

            # 5.【占用检测】云电脑已有连接时不触发自动连接，本轮直接跳过
            state = detect_desktop_state(page)
            log(f"5. 云电脑当前状态：{state}")
            if state.startswith("IN_USE"):
                log("   检测到云电脑已有连接（用户可能正在使用），本轮跳过保活，"
                    "等待下一个定时周期再判断。当前会话不受影响。")
                return 0

            log("6. 点击“进入AI云电脑”")
            page.locator("text=进入AI云电脑").first.click()

            log("7. 等待连接建立（若云电脑休眠中，唤醒需 2~3 分钟）")
            page.wait_for_url("**/desktop?id=**", timeout=45000)
            time.sleep(5)  # 等连接提示渲染出来再判断
            if wait_connected(page):
                log("   连接提示已消失，桌面应已连接")
            else:
                log("   WARN: 150 秒内未确认连接完成（可能仍在唤醒），继续尝试保持")

            log(f"8. 保持连接 {HOLD_SECONDS} 秒（每 10 秒移动一次鼠标，产生协议层输入）")
            deadline = time.time() + HOLD_SECONDS
            moves = 0
            while time.time() < deadline:
                page.mouse.move(600 + (moves % 8) * 6, 450)
                moves += 1
                time.sleep(10)
            log(f"   完成 {moves} 次鼠标输入，本次保活结束 ✓")

            # 9. 结束前最后核验：若仍在“连接中”，说明本次保活大概率没生效
            if connecting_indicator_visible(page):
                log("ERROR: 结束时页面仍显示连接中，本次保活可能未生效（请查看截图）")
                return 1
            return 0

        except Exception as e:
            log(f"ERROR: {e}")
            return 1
        finally:
            try:
                page.screenshot(path="screenshot.png")
                log("已保存截图 screenshot.png（在 Actions 的 Artifacts 里下载核验）")
            except Exception:
                pass
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
