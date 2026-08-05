"""
uia_sender.py — 基于 Windows UI Automation 的微信 4.0+ 消息发送器
=================================================================

原理：
  微信 4.0 基于 Electron (Chromium)。Chromium 通过 UIA 桥将 HTML 输入元素
  暴露为标准 UIA 控件。通过 ValuePattern 设置输入框文本，InvokePattern 点击
  发送按钮。全程无鼠标键盘模拟，无 DLL 注入，风控风险极低。

工作流：
  1. 定位微信 4.0 窗口 (Electron/Chromium)
  2. 搜索联系人 → 点击匹配项 → 切换到目标聊天
  3. 定位聊天输入框 (EditControl + ValuePattern)
  4. 设置文本 → 点击发送按钮或 Enter
  5. 图片通过剪贴板粘贴后发送

依赖:
  pip install uiautomation pyperclip
  发送图片需要 Pillow: pip install Pillow
"""

import logging
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("weflow-bridge")


class BaseSender:
    """消息发送器基类"""
    def send_text(self, contact: str, text: str) -> bool:
        raise NotImplementedError

    def send_image(self, contact: str, image_path: str) -> bool:
        raise NotImplementedError


class UiaSender(BaseSender):
    """
    基于 Windows UI Automation 的微信 4.0+ 发送器

    对微信 4.0 (Electron/Chromium) 优化：
      - 自动检测 Electron 架构
      - ValuePattern 直接设值（非键盘模拟）
      - InvokePattern 精确点击发送按钮
      - 自动联系人搜索切换

    Attributes:
        search_enabled: 是否自动搜索联系人（默认 True，False 则需手动切到聊天窗口）
    """

    WECHAT_TITLES = ["微信", "WeChat"]

    EXCLUDE_CLASSES = ["Chrome_WidgetWin_1", "CabinetWClass"]

    def __init__(self, search_enabled: bool = True):
        self._lock = threading.Lock()
        self._auto = None
        self._ready = False

        # 微信窗口
        self._window = None
        self._is_electron = False  # True=4.0+, False=3.9

        # 控件缓存
        self._search_box = None
        self._input_control = None
        self._send_button = None
        self._last_contact = ""
        self._use_coord_fallback = False

        self.search_enabled = search_enabled

        self._init()

    # ================================================================
    # 初始化
    # ================================================================

    def _init(self):
        """初始化 UIA 并定位窗口"""
        try:
            import uiautomation as auto
            self._auto = auto
        except ImportError:
            log.error("请先安装 uiautomation: pip install uiautomation")
            return

        log.info("正在搜索微信窗口...")
        self._find_window()
        if self._window:
            log.info(f"微信窗口: '{self._window.Name}' ClassName={self._window.ClassName}")
            self._ready = True

    def _find_window(self):
        """按标题/类名搜索微信窗口（优先个人微信，明确排除企业微信）"""
        auto = self._auto
        root = auto.GetRootControl()
        candidates = []
        for w in root.GetChildren():
            cls = w.ClassName
            name = w.Name or ""
            if cls in self.EXCLUDE_CLASSES:
                continue
            # 明确排除企业微信（标题含“企业微信”或类名为 WeWorkWindow），
            # 否则“企业微信”窗口因标题含“微信”会被误当成发送目标 → 发错窗口。
            if "企业微信" in name or cls == "WeWorkWindow":
                continue
            if cls == "Qt51514QWindowIcon":
                candidates.append(("qt", w))
            elif cls == "WeChatMainWndForPC":
                candidates.append(("old", w))
            elif "微信" in name:
                candidates.append(("title", w))
        if not candidates:
            self._window = None
            return
        # 优先级：微信 4.0 (Qt51514QWindowIcon) > 微信 3.9 (WeChatMainWndForPC) > 标题兜底
        for key, w in candidates:
            if key in ("qt", "old"):
                self._window = w
                self._is_electron = (key == "qt")
                return
        self._window = candidates[0][1]
        self._is_electron = True

    # ================================================================
    # 控件定位
    # ================================================================

    def _ensure_window(self) -> bool:
        """确保窗口可用"""
        if not self._ready:
            return False
        if self._window and self._window.Exists(0.2):
            return True
        self._find_window()
        if not self._window:
            log.warning("微信窗口未找到")
            self._ready = False
            return False
        return True

    def _activate(self):
        """激活微信窗口到前台（AttachThreadInput 确保后台也能生效）"""
        try:
            self._window.SetActive()
            time.sleep(0.3)
        except Exception:
            try:
                self._window.SwitchToThisWindow()
                time.sleep(0.3)
            except Exception:
                pass
        # AttachThreadInput 绕过 Windows 后台进程不能 SetForegroundWindow 的限制
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
            if not hwnd:
                hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
            if hwnd:
                WE_CHAT_TID = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
                CURRENT_TID = ctypes.windll.kernel32.GetCurrentThreadId()
                ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, True)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                ctypes.windll.user32.BringWindowToTop(hwnd)
                ctypes.windll.user32.AttachThreadInput(CURRENT_TID, WE_CHAT_TID, False)
        except Exception:
            pass

    # ================================================================
    # 后台友好：最小化/不抢焦点 支持
    # ================================================================

    def _find_hwnd(self):
        """获取微信主窗口句柄（兼容 Qt 4.0 与老版 WeChatMainWndForPC）"""
        import ctypes
        hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
        if not hwnd:
            hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
        return hwnd

    def _ensure_visible(self) -> bool:
        """
        若微信处于最小化状态则恢复为可见窗口（不抢前台）。

        返回是否原本处于最小化状态——调用方据此在结束时重新最小化，
        从而让微信可以一直最小化在后台跑。
        """
        try:
            import ctypes
            hwnd = self._find_hwnd()
            if not hwnd:
                return False
            if ctypes.windll.user32.IsIconic(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE = 9
                time.sleep(0.3)
                return True
        except Exception:
            pass
        return False

    def _restore_focus(self, prev_hwnd, was_minimized):
        """
        把前台焦点还给调用前的窗口；若微信原本最小化则重新最小化。
        这样 bot 回复时不会把微信永久弹到前台抢走用户焦点。
        """
        try:
            import ctypes
            if prev_hwnd:
                ctypes.windll.user32.SetForegroundWindow(prev_hwnd)
            if was_minimized:
                hwnd = self._find_hwnd()
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE = 6
        except Exception:
            pass

    def _force_foreground(self, hwnd):
        """
        强制把指定窗口提到前台（绕过 Windows 对「后台进程 SetForegroundWindow」的
        前台锁超时限制）。用于自动化发送前，确保微信窗口真正处于前台，使 UIA 的
        SetFocus / SendKeys 能正确落到微信而不是别的程序。

        采用 MSDN 推荐的可靠做法：
          1. 临时把系统 ForegroundLockTimeout 置 0（允许任意进程置前）；
          2. LockSetForegroundWindow 解锁；
          3. AttachThreadInput 把当前线程挂到目标线程输入队列 + SetForegroundWindow/
             BringWindowToTop/SetActiveWindow，并带重试；
          4. 恢复 ForegroundLockTimeout 为 200ms（不残留）。
        若窗口处于最小化，先 SW_RESTORE 恢复可见。
        """
        import ctypes
        SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2000
        SPIF_SENDCHANGE = 0x0002
        LSFW_UNLOCK = 2
        SW_RESTORE = 9
        try:
            if ctypes.windll.user32.IsIconic(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.25)
            # 临时关闭前台锁超时，允许任意进程置前
            try:
                ctypes.windll.user32.SystemParametersInfoW(
                    SPI_SETFOREGROUNDLOCKTIMEOUT, 0, None, SPIF_SENDCHANGE)
            except Exception:
                pass
            try:
                ctypes.windll.user32.LockSetForegroundWindow(LSFW_UNLOCK)
            except Exception:
                pass
            TID = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
            CID = ctypes.windll.kernel32.GetCurrentThreadId()
            ctypes.windll.user32.AttachThreadInput(CID, TID, True)
            try:
                for _ in range(3):
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    ctypes.windll.user32.BringWindowToTop(hwnd)
                    try:
                        ctypes.windll.user32.SetActiveWindow(hwnd)
                    except Exception:
                        pass
                    if ctypes.windll.user32.GetForegroundWindow() == hwnd:
                        break
                    time.sleep(0.08)
            finally:
                ctypes.windll.user32.AttachThreadInput(CID, TID, False)
        except Exception as e:
            log.warning(f"强制置前失败(可忽略): {e}")
        finally:
            # 恢复前台锁超时，避免影响其他程序抢焦点
            try:
                ctypes.windll.user32.SystemParametersInfoW(
                    SPI_SETFOREGROUNDLOCKTIMEOUT, 200, None, SPIF_SENDCHANGE)
            except Exception:
                pass
            time.sleep(0.2)

    @contextmanager
    def _wechat_foreground(self):
        """
        上下文管理器：在 with 块期间保证微信处于前台，退出时把焦点还给调用前的窗口。

        为什么需要它：
            UIA 的 SetFocus / SendKeys / 坐标点击只在「目标窗口是前台」时才真正生效。
            当微信在后台（用户正在用别的程序）时，这些操作会静默失败或打到其他窗口。
            本管理器在块开始时把微信临时置前（AttachThreadInput 绕过后台置前限制）、
            若原本最小化则先恢复可见；块结束时还原原前台窗口、必要时重新最小化，
            从而做到「后台也能回复，且不会长期抢走用户焦点」（仅一次瞬闪）。

        嵌套安全：若进入时微信本就是前台（prev==hwnd），则完全透传、不还原。
        """
        import ctypes
        hwnd = self._find_hwnd()
        prev = ctypes.windll.user32.GetForegroundWindow()
        was_min = self._ensure_visible()
        did_fg = False
        if hwnd and prev != hwnd:
            try:
                self._force_foreground(hwnd)
                did_fg = True
            except Exception:
                pass
        try:
            yield
        finally:
            if did_fg and prev:
                try:
                    ctypes.windll.user32.SetForegroundWindow(prev)
                except Exception:
                    pass
            if was_min and hwnd:
                try:
                    ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE = 6
                except Exception:
                    pass

    def _press_enter_background(self, ctrl) -> bool:
        """
        后台方式发送回车：向输入框原生窗口 PostMessage WM_KEYDOWN/UP(Enter)。
        无需前台、无需移动鼠标。成功返回 True。
        """
        try:
            import ctypes
            hwnd = getattr(ctrl, "NativeWindowHandle", 0) or 0
            if not hwnd:
                return False
            VK_RETURN = 0x0D
            WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
            ctypes.windll.user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0)
            time.sleep(0.05)
            ctypes.windll.user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0)
            return True
        except Exception:
            return False

    def _send_enter_fallback(self, ctrl):
        """
        PostMessage 失败时的兜底：短暂把微信提到前台、用 SendKeys 发回车，
        随后立即把焦点还给原窗口。
        """
        import ctypes
        prev = ctypes.windll.user32.GetForegroundWindow()
        try:
            hwnd = self._find_hwnd()
            if hwnd and prev != hwnd:
                self._force_foreground(hwnd)
            else:
                time.sleep(0.1)
            try:
                ctrl.SetFocus()
                time.sleep(0.05)
            except Exception:
                pass
            ctrl.SendKeys('{Enter}')
        finally:
            if prev and (not hwnd or prev != hwnd):
                try:
                    ctypes.windll.user32.SetForegroundWindow(prev)
                except Exception:
                    pass

    def _dump_tree(self, ctrl, depth: int = 0, max_depth: int = 4):
        """调试: 输出 UIA 子树（仅 debug）"""
        if depth > max_depth:
            return
        try:
            pad = "  " * depth
            name = (ctrl.Name or "")[:40]
            cls = ctrl.ClassName or ""
            ctrl_type = ctrl.ControlTypeName
            vp = ctrl.IsValuePatternAvailable if hasattr(ctrl, 'IsValuePatternAvailable') else '?'
            ip = ctrl.IsInvokePatternAvailable if hasattr(ctrl, 'IsInvokePatternAvailable') else '?'
            rect = ctrl.BoundingRectangle
            info = f"[{rect.left},{rect.top} {rect.width()}x{rect.height()}]" if rect else ""
            log.debug(f"{pad}{ctrl_type} '{name}' {info} V={vp} I={ip} cls={cls}")
            for child in ctrl.GetChildren():
                self._dump_tree(child, depth + 1, max_depth)
        except Exception:
            pass

    def _find_search_box_uia(self):
        """
        通过 UIA 树定位微信搜索框。

        微信 4.x (Qt/Electron) 的搜索框特征：
        - EditControl 类型
        - 窗口上半部分 (top < 30% 窗口高度)
        - 宽度小于窗口一半（区别于底部的聊天输入框）
        - 宽度大于 50px（排除小控件）
        """
        auto = self._auto
        win_rect = self._window.BoundingRectangle
        win_w = win_rect.width()
        win_h = win_rect.height()

        edits = []

        def walk(ctrl, depth=0):
            if depth > 12:
                return
            try:
                for child in ctrl.GetChildren():
                    if child.ControlTypeName == "EditControl":
                        rect = child.BoundingRectangle
                        if rect and rect.width() > 50:
                            edits.append((child, rect))
                    walk(child, depth + 1)
            except Exception:
                pass

        try:
            walk(self._window)
        except Exception:
            pass

        # 过滤：上半部分的 EditControl，宽度小于窗口一半
        candidates = [
            (c, r) for c, r in edits
            if r.top < win_rect.top + win_h * 0.3 and r.width() < win_w * 0.5
        ]

        if not candidates:
            return None

        # 取最靠上的（搜索框通常比任何其他上半部分控件更高）
        candidates.sort(key=lambda x: x[1].top)
        return candidates[0][0]

    def _focus_chat_input(self):
        """
        物理点击聊天输入框区域（坐标后备模式专用）。
        让聊天输入框获得键盘焦点。
        """
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return

        hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
        if not hwnd:
            hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
        if not hwnd:
            return

        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        input_x = rect.left + int(win_w * 0.3)
        input_y = rect.top + int(win_h * 0.92)
        ctypes.windll.user32.SetCursorPos(input_x, input_y)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.3)

    def _switch_contact(self, contact: str) -> bool:
        """
        切换到指定联系人/群聊的聊天窗口。

        优先用 UIA 直接操作搜索框切换（比 Ctrl+F 键盘模拟可靠得多）。
        整段在 _wechat_foreground 上下文内运行：进入时把微信置前、退出时还原焦点，
        从而保证 UIA 的 SetFocus/SendKeys 与键盘模拟都真正落到微信窗口——
        即使微信在后台（用户正在用别的程序），也能正确切到目标会话。
        """
        if not self._ensure_window():
            return False

        with self._wechat_foreground():
            # 优先用 UIA 直接操作搜索框切换（比 Ctrl+F 键盘模拟可靠得多）
            if self._switch_contact_uia(contact):
                return True

            try:
                import ctypes
                from ctypes import wintypes
            except ImportError:
                return False

            hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
            if not hwnd:
                hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
            if not hwnd:
                log.warning("找不到微信主窗口句柄")
                return False

            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            win_w = rect.right - rect.left
            win_h = rect.bottom - rect.top

            try:
                # Ctrl+F 打开搜索
                ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)   # Ctrl
                ctypes.windll.user32.keybd_event(0x46, 0, 0, 0)   # F
                ctypes.windll.user32.keybd_event(0x46, 0, 2, 0)
                ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
                time.sleep(0.5)

                # 清空搜索框
                ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)   # Ctrl
                ctypes.windll.user32.keybd_event(0x41, 0, 0, 0)   # A
                ctypes.windll.user32.keybd_event(0x41, 0, 2, 0)
                ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
                time.sleep(0.15)

                # 粘贴联系人/群名
                import pyperclip
                pyperclip.copy(contact)
                time.sleep(0.1)
                ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)   # Ctrl
                ctypes.windll.user32.keybd_event(0x56, 0, 0, 0)   # V
                ctypes.windll.user32.keybd_event(0x56, 0, 2, 0)
                ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
                time.sleep(0.3)

                # Enter → 选中第一个结果
                ctypes.windll.user32.keybd_event(0x0D, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x0D, 0, 2, 0)
                time.sleep(0.8)

                log.info(f"已切到联系人: {contact}")
                return True
            finally:
                # 焦点还原交由外层 _wechat_foreground 上下文处理，这里无需操作
                pass

    def _switch_contact_uia(self, contact: str) -> bool:
        """
        用 UIA 直接操作顶部搜索框切换到目标会话（联系人/群聊）。

        定位顶部搜索框 EditControl → 清空 → 填入昵称/群名 → Enter 选中第一个结果。
        调用方（_switch_contact）已通过 _wechat_foreground 上下文把微信置前，
        因此这里的 SetFocus / SendKeys 能真正落到微信窗口；可见性/最小化也由
        上下文负责，本函数不再自行处理。
        """
        try:
            search = self._find_search_box_uia()
            if search is None:
                log.info("UIA 未找到搜索框，跳过 UIA 切换")
                return False
            log.info(f"UIA 切换会话: {contact}")
            try:
                search.SetFocus()
                time.sleep(0.15)
            except Exception:
                pass
            # 清空搜索框
            try:
                search.SetValue("")
            except Exception:
                pass
            time.sleep(0.1)
            # 填入目标昵称/群名
            import pyperclip
            pyperclip.copy(contact)
            time.sleep(0.1)
            try:
                search.SendKeys("{Ctrl}a")
                time.sleep(0.05)
                search.SendKeys("{Delete}")
                time.sleep(0.05)
            except Exception:
                pass
            search.SendKeys("{Ctrl}v")
            time.sleep(0.7)
            # 选中第一个搜索结果（最近/最匹配会话）
            search.SendKeys("{Enter}")
            time.sleep(0.7)
            return True
        except Exception as e:
            log.warning(f"UIA 切换失败: {e}")
            return False

    def _locate_input(self) -> bool:
        """
        定位聊天输入框和发送按钮。

        微信 4.0 基于 Qt（窗口类 Qt51514QWindowIcon），聊天输入框通常暴露为
        EditControl 或 DocumentControl，且位于窗口下半部分。此前只认 EditControl
        导致一直找不到 → 退化成"坐标点击"这种不可靠方式。这里放宽到
        EditControl / DocumentControl，能直接定位就用 SetValue，避免盲点坐标。
        """
        if not self._ensure_window():
            return False

        # 如果已有缓存且窗口没变，直接返回
        if self._input_control is not None:
            try:
                self._input_control.GetCurrentPattern()
                return True
            except Exception:
                self._input_control = None
                self._send_button = None

        auto = self._auto
        win_rect = self._window.BoundingRectangle
        win_center_y = win_rect.top + win_rect.height() / 2

        # 微信输入框可能的 UIA 控件类型
        INPUT_TYPES = ("EditControl", "DocumentControl")

        edits = []

        def walk(ctrl, depth=0):
            if depth > 14:
                return
            try:
                for child in ctrl.GetChildren():
                    try:
                        cn = child.ControlTypeName
                        if cn in INPUT_TYPES:
                            rect = child.BoundingRectangle
                            if rect and rect.width() > 100:
                                edits.append(child)
                        walk(child, depth + 1)
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            walk(self._window)
        except Exception as e:
            log.debug(f"UIA 遍历异常: {e}")

        if not edits:
            log.warning("未找到输入控件，使用坐标后备方案（Qt 界面）")
            self._use_coord_fallback = True
            return True

        # 过滤：聊天输入框在窗口下半部分，面积较大（排除顶部的消息记录区）
        candidates = [e for e in edits
                      if e.BoundingRectangle and
                      e.BoundingRectangle.top >= win_center_y - 20 and
                      e.BoundingRectangle.width() > 100]

        if not candidates:
            candidates = [e for e in edits if e.BoundingRectangle]

        # 按面积倒序，最大的就是聊天输入框
        candidates.sort(key=lambda e: e.BoundingRectangle.width() *
                        e.BoundingRectangle.height(), reverse=True)

        for ctrl in candidates:
            rect = ctrl.BoundingRectangle
            area = rect.width() * rect.height()
            if area < 200:
                continue

            name = ctrl.Name or ""
            has_vp = ctrl.IsValuePatternAvailable
            has_tp = ctrl.IsTextPatternAvailable if hasattr(ctrl, "IsTextPatternAvailable") else False
            log.debug(f"输入候选: '{name[:30]}' {rect.width()}x{rect.height()} "
                      f"V={has_vp} T={has_tp}")

            # 优先使用支持 ValuePattern 的（可直接设值，最可靠）
            if has_vp:
                self._input_control = ctrl
                log.info(f"聊天输入框: {rect.width()}x{rect.height()} (ValuePattern)")
                break
            # 其次：支持 TextPattern 的（用粘贴方式写入）
            if has_tp:
                self._input_control = ctrl
                log.info(f"聊天输入框: {rect.width()}x{rect.height()} (TextPattern)")
                break

        if not self._input_control:
            # 后备：用面积最大的
            self._input_control = candidates[0] if candidates else edits[0]
            log.warning(f"输入框无 ValuePattern/TextPattern，使用 SendKeys 后备方案")
            log.debug(f"后备输入控件: {self._input_control.ControlTypeName} "
                      f"'{self._input_control.Name[:30]}'")

        # 查找发送按钮
        try:
            buttons = []

            def find_buttons(ctrl, depth=0):
                if depth > 8:
                    return
                try:
                    for child in ctrl.GetChildren():
                        if child.ControlTypeName == "ButtonControl":
                            bn = child.Name or ""
                            if "发送" in bn or "Send" in bn or bn.strip() == "":
                                buttons.append(child)
                        find_buttons(child, depth + 1)
                except Exception:
                    pass

            find_buttons(self._window)
            if buttons:
                self._send_button = buttons[0]
                log.info("已定位发送按钮")
            else:
                log.info("未找到发送按钮，发送时用 Enter")
        except Exception:
            pass

        return True

    # ================================================================
    # 发送方法
    # ================================================================

    def send_text(self, contact: str, text: str) -> bool:
        """
        发送文本消息

        Args:
            contact: 联系人昵称/备注
            text: 消息内容
        """
        with self._lock:
            if not self._ready:
                log.error("UIA Sender 未就绪")
                return False

            if not self._ensure_window():
                return False

            # 每次发送都重置输入框定位缓存，强制在「切换会话之后」重新定位输入框。
            # 否则会复用上一次会话的陈旧控件，SetValue/SendKeys 落到旧会话的输入框，
            # 造成「回复发到错误的 / 当前的窗口」（用户反馈的核心问题）。
            self._input_control = None
            self._use_coord_fallback = False

            # 安全检查：过滤 PIL 引用
            if "<PIL." in text or "PIL." in text:
                log.warning(f"跳过 PIL 引用消息: {text[:60]}")
                return False

            # 后台友好：把整个「切会话→定位输入框→发消息」包进 _wechat_foreground
            # 上下文。进入时把微信置前（若最小化则先恢复可见），退出时把焦点还给
            # 用户原来的窗口（必要时重新最小化）。这样即使微信在后台（用户正在用
            # 别的程序），UIA 的 SetFocus/SendKeys/坐标点击也真正落到微信窗口；
            # 回复结束后焦点立即还原，不会长期抢走用户注意力。
            with self._wechat_foreground():
                try:
                    # 切换到联系人（每次都切，确保回复到正确的会话窗口，
                    # 不受"用户中途手动切走窗口"影响）
                    if self.search_enabled and contact:
                        if not self._switch_contact(contact):
                            log.warning(f"无法自动切换到 '{contact}'，尝试在当前窗口发送")

                    # 定位输入框
                    if not self._locate_input():
                        return False

                    if self._use_coord_fallback:
                        # Qt 界面：点击输入框区域→清空→剪贴板粘贴→Enter
                        import pyperclip
                        import ctypes
                        from ctypes import wintypes
                        hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
                        if not hwnd:
                            hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
                        if hwnd:
                            rect = wintypes.RECT()
                            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                            win_w = rect.right - rect.left
                            win_h = rect.bottom - rect.top
                            # 输入框大致在窗口底部居中偏左的位置
                            input_x = rect.left + int(win_w * 0.3)
                            input_y = rect.top + int(win_h * 0.92)
                            # 物理点击让输入框获得焦点（PostMessage 对 Qt 子控件无效）
                            ctypes.windll.user32.SetCursorPos(input_x, input_y)
                            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # down
                            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # up
                            time.sleep(0.2)
                            # 先全选清空，避免旧文本残留 / 误发到错误内容
                            self._auto.SendKeys('{Ctrl}a')
                            time.sleep(0.05)
                            self._auto.SendKeys('{Delete}')
                            time.sleep(0.1)
                        time.sleep(0.2)
                        pyperclip.copy(text)
                        time.sleep(0.05)
                        self._auto.SendKeys('{Ctrl}v')
                        time.sleep(0.3)
                        self._auto.SendKeys('{Enter}')
                        log.info(f"[UIA✓] {contact}: {text[:50]}... (无鼠标模式)")
                        return True

                    ctrl = self._input_control

                    # 让输入框获得焦点，后续 SendKeys 才准确落到该控件
                    try:
                        ctrl.SetFocus()
                        time.sleep(0.1)
                    except Exception:
                        pass

                    # 设置文本（ValuePattern 可直接设值，无需前台）
                    if ctrl.IsValuePatternAvailable:
                        try:
                            ctrl.SetValue(text)
                        except Exception as e:
                            log.warning(f"SetValue 失败: {e}，尝试剪贴板")
                            import pyperclip
                            pyperclip.copy(text)
                            time.sleep(0.05)
                            ctrl.SendKeys('{Ctrl}a')
                            time.sleep(0.05)
                            ctrl.SendKeys('{Ctrl}v')
                    else:
                        import pyperclip
                        try:
                            ctrl.SendKeys('{Ctrl}a')
                            time.sleep(0.05)
                            ctrl.SendKeys('{Delete}')
                            time.sleep(0.05)
                        except Exception:
                            pass
                        pyperclip.copy(text)
                        time.sleep(0.05)
                        ctrl.SendKeys('{Ctrl}v')

                    time.sleep(0.1)

                    # 发送（优先后台安全方式：InvokePattern / PostMessage，避免抢前台或移动鼠标）
                    if self._send_button:
                        try:
                            self._send_button.Invoke()
                        except Exception:
                            self._send_button.Click()
                    else:
                        if not self._press_enter_background(ctrl):
                            self._send_enter_fallback(ctrl)

                    log.info(f"[UIA✓] {contact}: {text[:50]}...")
                    return True

                except Exception as e:
                    log.error(f"[UIA✗] {contact}: {e}")
                    return False

    def send_image(self, contact: str, image_path: str) -> bool:
        """
        通过剪贴板发送图片

        Args:
            contact: 联系人
            image_path: 图片文件路径
        """
        with self._lock:
            if not self._ready:
                return False
            if not os.path.isfile(image_path):
                log.error(f"图片不存在: {image_path}")
                return False

            try:
                if not self._ensure_window():
                    return False

                # 每次发送都重置输入框定位缓存，避免跨会话复用陈旧控件（回复发错窗口）
                self._input_control = None
                self._use_coord_fallback = False

                # 后台友好：把整个「切会话→粘贴图片→发送」包进 _wechat_foreground
                # 上下文，进入时把微信置前（若最小化则先恢复可见）、退出时把焦点
                # 还给用户原来的窗口（必要时重新最小化）。这样即使微信在后台，
                # UIA 的 SendKeys/坐标点击也真正落到微信窗口，且不会长期抢焦点。
                with self._wechat_foreground():
                    if self.search_enabled and contact:
                        self._switch_contact(contact)

                    # 复制图片到剪贴板
                    self._copy_image_to_clipboard(image_path)
                    time.sleep(0.2)

                    if not self._locate_input():
                        return False

                    if self._use_coord_fallback:
                        import ctypes
                        from ctypes import wintypes
                        hwnd = ctypes.windll.user32.FindWindowW('Qt51514QWindowIcon', None)
                        if not hwnd:
                            hwnd = ctypes.windll.user32.FindWindowW('WeChatMainWndForPC', None)
                        if hwnd:
                            rect = wintypes.RECT()
                            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                            input_x = rect.left + int((rect.right - rect.left) * 0.3)
                            input_y = rect.top + int((rect.bottom - rect.top) * 0.92)
                            ctypes.windll.user32.SetCursorPos(input_x, input_y)
                            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
                        time.sleep(0.3)
                        self._auto.SendKeys('{Ctrl}v')
                        time.sleep(0.5)
                        self._auto.SendKeys('{Enter}')
                        log.info(f"[UIA✓] 图片 → {contact}: {os.path.basename(image_path)} (无鼠标模式)")
                        return True

                    self._input_control.SendKeys('{Ctrl}v')
                    time.sleep(0.5)

                    if self._send_button:
                        try:
                            self._send_button.Invoke()
                        except Exception:
                            self._send_button.Click()
                    else:
                        if not self._press_enter_background(self._input_control):
                            self._send_enter_fallback(self._input_control)

                    log.info(f"[UIA✓] 图片 → {contact}: {os.path.basename(image_path)}")
                    return True

            except Exception as e:
                log.error(f"[UIA✗] 图片 → {contact}: {e}")
                return False

    def _copy_image_to_clipboard(self, path: str):
        """复制图片到剪贴板（通过 PowerShell，避免 PIL 对象被当作文本复制）"""
        abs_path = os.path.abspath(path)
        try:
            subprocess.run([
                "powershell", "-WindowStyle", "Hidden", "-Command",
                f"Add-Type -AssemblyName System.Windows.Forms;"
                f"$img = [System.Drawing.Image]::FromFile('{abs_path}');"
                f"[System.Windows.Forms.Clipboard]::SetImage($img);"
                f"$img.Dispose()"
            ], check=True, timeout=10)
            log.debug("PowerShell 已复制图片到剪贴板")
        except Exception as e:
            log.error(f"复制图片到剪贴板失败: {e}")
            raise

    # ================================================================
    # 诊断
    # ================================================================

    def diagnose(self):
        """输出诊断信息，用于调试"""
        if not self._window:
            print("✗ 未找到微信窗口")
            return

        print(f"✓ 微信窗口: '{self._window.Name}'")
        print(f"  ClassName: {self._window.ClassName}")
        print(f"  Electron: {self._is_electron}")
        print(f"  位置: [{self._window.BoundingRectangle.left},"
              f"{self._window.BoundingRectangle.top}] "
              f"{self._window.BoundingRectangle.width()}x"
              f"{self._window.BoundingRectangle.height()}")

        print("\n--- UIA 树 ---")
        self._dump_tree(self._window, max_depth=4)

        print("\n--- 控件状态 ---")
        print(f"  输入框: {'✓' if self._input_control else '✗'}")
        print(f"  发送按钮: {'✓' if self._send_button else '✗'}")
