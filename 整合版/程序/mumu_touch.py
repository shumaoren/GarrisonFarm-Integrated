"""MuMu SDK touch adapter; loaded after the original GarrisonFarm setup."""
import atexit
import ctypes
import json
import os
import time


class MuMuTouch:
    def __init__(self, config, device):
        self.handle = 0
        self.held = False
        self.display = None
        self.last = None
        self.interval = max(0.008, float(config.get('move_interval', 0.016)))
        root = config['emulator_path']
        path = config.get('sdk_path') or os.path.join(root, 'nx_main', 'sdk', 'external_renderer_ipc.dll')
        if not os.path.isfile(path):
            raise RuntimeError('MuMu SDK 不存在: ' + path)
        self.dll = ctypes.CDLL(path)
        signatures = {
            'nemu_connect': [ctypes.c_wchar_p, ctypes.c_int],
            'nemu_disconnect': [ctypes.c_int],
            'nemu_get_display_id': [ctypes.c_int, ctypes.c_char_p, ctypes.c_int],
            'nemu_capture_display': [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.c_void_p],
            'nemu_input_event_finger_touch_down': [ctypes.c_int]*5,
            'nemu_input_event_finger_touch_up': [ctypes.c_int]*3,
        }
        for name, args in signatures.items():
            fn = getattr(self.dll, name)
            fn.argtypes = args
            fn.restype = ctypes.c_int
        index = config.get('instance_index')
        if index is None:
            port = int(str(device).rsplit(':', 1)[1])
            if port < 16384 or (port-16384) % 32:
                raise RuntimeError('无法由 ADB 地址确定 MuMu 实例，请设置 instance_index')
            index = (port-16384)//32
        self.index = int(index)
        try:
            self.handle = self.dll.nemu_connect(root, self.index)
            if not self.handle:
                raise RuntimeError('MuMu SDK 连接失败，实例 ' + str(self.index))
            candidates = config.get('_package_candidates') or [config.get('package_name', 'com.hypergryph.arknights')]
            matches = []
            for package in candidates:
                display = self.dll.nemu_get_display_id(self.handle, package.encode('utf-8'), 0)
                if display >= 0:
                    matches.append((package, display))
            if not matches:
                raise RuntimeError('未找到明日方舟画面，请打开官服或B服游戏')
            if len(matches) > 1:
                raise RuntimeError('官服和B服都在运行但无法确定前台，请把要刷的游戏切到前台后重试')
            self.package, self.display = matches[0]
            print('[自动检测] 当前游戏: ' + self.package, flush=True)
            w, h = ctypes.c_int(), ctypes.c_int()
            ret = self.dll.nemu_capture_display(self.handle, self.display, 0, ctypes.byref(w), ctypes.byref(h), None)
            if ret or (w.value, h.value) != (1920, 1080):
                raise RuntimeError('MuMu 游戏画面尺寸不匹配: %sx%s (ret=%s)' % (w.value, h.value, ret))
            self.width, self.height = w.value, h.value
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            try:
                if self.held:
                    self.dll.nemu_input_event_finger_touch_up(self.handle, self.display, 1)
            finally:
                self.held = False
                self.dll.nemu_disconnect(self.handle)
                self.handle = 0

    def _point(self, x, y):
        x, y = int(x), int(y)
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError('触控坐标超出画面: %s,%s' % (x, y))
        return x, y

    def _down(self, x, y):
        x, y = self._point(x, y)
        self.held = True  # Release even if the native call reports a partial failure.
        ret = self.dll.nemu_input_event_finger_touch_down(self.handle, self.display, 1, x, y)
        if ret:
            self._abort('MuMu 触控失败: ' + str(ret))
        self.last = (x, y)

    def _up(self):
        if self.held:
            ret = self.dll.nemu_input_event_finger_touch_up(self.handle, self.display, 1)
            if ret:
                self._abort('MuMu 松手失败: ' + str(ret))
            self.held = False

    def _abort(self, message):
        try:
            self.close()
        finally:
            raise RuntimeError(message + '；已停止，避免在拖动中切换触控后端')

    def touch(self, kind, x, y):
        kind = str(kind).upper()
        if kind == 'DOWN':
            if self.held:
                self._up()
            self._down(x, y)
            time.sleep(0.06)
        elif kind == 'MOVE':
            if not self.held:
                raise RuntimeError('MOVE 前没有按下')
            self._down(x, y)
        elif kind == 'UP':
            self._up()
        else:
            raise ValueError('未知触控事件: ' + kind)

    def drag_path(self, x1, y1, x2, y2, steps=12):
        if not self.held:
            raise RuntimeError('拖动前没有按下')
        self._point(x1, y1)
        self._point(x2, y2)
        steps = max(1, int(steps))
        try:
            for i in range(1, steps+1):
                self._down(x1+(x2-x1)*i/steps, y1+(y2-y1)*i/steps)
                time.sleep(self.interval)
        except BaseException:
            self._up()
            raise

    def tap(self, x, y, wait=0.6):
        try:
            self.touch('DOWN', x, y)
        finally:
            self._up()
        time.sleep(wait)

    def swipe(self, x1, y1, x2, y2, ms=500, wait=0.8):
        try:
            self.touch('DOWN', x1, y1)
            steps = max(1, round(float(ms)/1000/self.interval))
            self.drag_path(x1, y1, x2, y2, steps)
        finally:
            self._up()
        time.sleep(wait)


def install(namespace):
    """Only swap all four primitives after successful connection and validation."""
    _install_selection_rules(namespace)
    _install_promotion_priority(namespace)
    mode = os.environ.get('GF_LAUNCH_MODE')
    if mode == 'promotion':
        try:
            _load_local_module(namespace, 'battle_screen')
            _load_local_module(namespace, 'level1_promotion')
            _load_local_module(namespace, 'promotion_runner').install(namespace)
        except Exception as exc:
            raise SystemExit(f'[刷晋升] 安装失败：{exc}') from exc
    path = os.path.join(namespace['HERE'], 'mumu_touch.json')
    try:
        with open(path, encoding='utf-8-sig') as f:
            config = json.load(f)
        if not config.get('enabled', True):
            print('[增强触控] 已关闭，使用原 ADB 触控', flush=True)
            return
        config = _resolve_environment(config, namespace)
        controller = MuMuTouch(config, config['_device'])
    except Exception as exc:
        print('[增强触控] 初始化失败，保留原 ADB 触控: ' + str(exc), flush=True)
        return
    atexit.register(controller.close)
    namespace['_mumu_touch_controller'] = controller
    namespace.update({name: getattr(controller, name) for name in ('tap', 'swipe', 'touch', 'drag_path')})
    enable_fast_capture(controller, namespace, config)
    print('[增强触控] MuMu SDK 已启用，实例 %s，display %s；保留原截图和 OCR' % (controller.index, controller.display), flush=True)


def _load_local_module(namespace, name):
    import importlib.util
    import sys

    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    path = os.path.join(namespace['HERE'], name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def _run_with_ocr(namespace, gpu):
    import sys
    from types import CodeType, FunctionType

    gpu.install(namespace, gpu.read_settings(namespace['HERE']))
    # The bundled main's --mumu-check branch provides an entry before OCR setup.
    # Dispatch to the original main after installing the runtime configuration.
    functions = [code for code in namespace['main'].__code__.co_consts
                 if isinstance(code, CodeType) and code.co_name == 'main' and code.co_argcount == 0]
    if len(functions) != 1 or '--check' not in functions[0].co_consts:
        raise RuntimeError('主程序版本和整合入口不匹配')
    sys.argv.remove('--mumu-check')
    FunctionType(functions[0], namespace)()


def probe(namespace):
    """Read-only SDK connection check; never sends touch events."""
    mode = os.environ.get('GF_LAUNCH_MODE')
    if mode in ('gpu-list', 'gpu-test', 'promotion-replay', 'medal', 'promotion'):
        try:
            gpu = _load_local_module(namespace, 'ocr_gpu')
            if mode == 'gpu-list':
                print('GPU_JSON=' + json.dumps(gpu.adapters(), ensure_ascii=True), flush=True)
                os._exit(0)
            elif mode == 'gpu-test':
                gpu.test_ocr(namespace)
                os._exit(0)
            elif mode == 'promotion-replay':
                _load_local_module(namespace, 'battle_screen')
                _load_local_module(namespace, 'level1_promotion')
                _load_local_module(namespace, 'promotion_runner').replay(namespace, gpu)
                os._exit(0)
            else:
                _run_with_ocr(namespace, gpu)
            return
        except Exception as exc:
            print(f'[GarrisonFarm] {exc}', flush=True)
            if mode in ('gpu-list', 'gpu-test', 'promotion-replay'):
                os._exit(1)
            raise SystemExit(1) from exc
    with open(os.path.join(namespace['HERE'], 'mumu_touch.json'), encoding='utf-8-sig') as f:
        config = json.load(f)
    config = _resolve_environment(config, namespace)
    controller = MuMuTouch(config, config['_device'])
    try:
        print('[增强触控检查] SDK 连接成功，实例 %s，display %s，画面 %sx%s；未发送触控事件' % (controller.index, controller.display, controller.width, controller.height), flush=True)
    finally:
        controller.close()
    if mode == 'touch-check':
        os._exit(0)




def enable_fast_capture(controller, namespace, config):
    import threading
    lock = threading.RLock()
    np = namespace['np']
    width, height = controller.width, controller.height
    buffer = (ctypes.c_ubyte * (width*height*4))()

    def capture():
        with lock:
            w, h = ctypes.c_int(width), ctypes.c_int(height)
            ret = controller.dll.nemu_capture_display(controller.handle, controller.display, len(buffer), ctypes.byref(w), ctypes.byref(h), buffer)
            if ret or (w.value,h.value)!=(width,height):
                raise RuntimeError('MuMu SDK 截图失败或尺寸改变: '+str(ret))
            rgba=np.ctypeslib.as_array(buffer).reshape(height,width,4)
            return np.ascontiguousarray(rgba[::-1,:,2::-1])

    if config.get('fast_capture',True):
        namespace['shot']=capture
        print('[快速截图] MuMu SDK 已启用，替代 ADB 截图',flush=True)


def _sdk_location(hint):
    if not hint:
        return None
    hint=os.path.abspath(os.path.expandvars(str(hint).strip('"')))
    directory=hint if os.path.isdir(hint) else os.path.dirname(hint)
    for _ in range(7):
        for rel in ('nx_main/sdk/external_renderer_ipc.dll','nx_device/15.0/shell/sdk/external_renderer_ipc.dll','nx_device/12.0/shell/sdk/external_renderer_ipc.dll','shell/sdk/external_renderer_ipc.dll','sdk/external_renderer_ipc.dll'):
            sdk=os.path.join(directory,*rel.split('/'))
            if os.path.isfile(sdk):
                # When starting from nx_main or a version shell, prefer an actual installation root.
                if rel.startswith('sdk/') and os.path.basename(directory).lower() in ('nx_main','shell'):
                    continue
                root=directory
                if os.path.basename(os.path.dirname(root)).lower()=='nx_device':
                    root=os.path.dirname(os.path.dirname(root))
                return root,sdk
        parent=os.path.dirname(directory)
        if parent==directory:break
        directory=parent
    return None


def _read_command(args,timeout=5):
    import subprocess
    try:
        r=subprocess.run(args,capture_output=True,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return r.stdout.decode('utf-8',errors='replace') if r.returncode==0 else ''
    except (OSError,subprocess.TimeoutExpired):
        return ''


def _running_mumu_paths():
    text=_read_command(['powershell.exe','-NoProfile','-Command',"[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Process MuMuNxDevice,MuMuPlayer,MuMuPlayerService -ErrorAction SilentlyContinue | ForEach-Object { $_.Path }"],timeout=6)
    return [s.strip() for s in text.splitlines() if s.strip()]


def _maa_connections(namespace):
    try:
        maa=namespace['find_maa']()
        if maa:
            adb,address,_,_=namespace['read_maa_connect'](maa)
            return adb,address
    except Exception:
        pass
    return '',''


def _resolve_environment(config,namespace):
    import re
    result=dict(config)
    adb=namespace.get('ADB') or ''
    device=namespace.get('DEV') or ''
    maa_adb,maa_device=('','')
    if not adb or not device:
        maa_adb,maa_device=_maa_connections(namespace)
        adb=adb or maa_adb
        device=device or maa_device
    # The already-connected ADB is the strongest installation hint.
    location=_sdk_location(adb)
    if location is None:
        locations=[]
        for hint in _running_mumu_paths():
            candidate=_sdk_location(hint)
            if candidate and candidate not in locations:locations.append(candidate)
        if len(locations)==1:location=locations[0]
        elif len(locations)>1:
            manual=_sdk_location(config.get('emulator_path'))
            if manual in locations:location=manual
            else:raise RuntimeError('检测到多套正在运行的MuMu，请在MAA中设置目标实例的ADB路径')
    if location is None:
        for drive in 'CDEFGHIJK':
            for suffix in ('MuMuPlayer','Program Files/Netease/MuMu','Program Files/Netease/MuMuPlayer-12.0','Program Files/MuMuPlayer-12.0'):
                location=_sdk_location(drive+':/'+suffix)
                if location:break
            if location:break
    if location is None:location=_sdk_location(config.get('emulator_path'))
    if location is None:
        raise RuntimeError('未找到MuMu SDK，请打开模拟器并确认MAA连接；也可填写emulator_path')
    root,sdk=location
    result['emulator_path']=root
    result['sdk_path']=sdk
    if not adb:
        for rel in ('nx_main/adb.exe','nx_device/15.0/shell/adb.exe','nx_device/12.0/shell/adb.exe','shell/adb.exe'):
            candidate=os.path.join(root,*rel.split('/'))
            if os.path.isfile(candidate):adb=candidate;break
    if not device and adb:
        devices=[]
        for line in _read_command([adb,'devices']).splitlines():
            parts=line.split()
            if len(parts)==2 and parts[1]=='device':devices.append(parts[0])
        if len(devices)==1:device=devices[0]
    if not device:
        raise RuntimeError('未确定目标模拟器实例，请先在MAA中连接目标模拟器')
    result['_device']=device
    packages=[]
    foreground=''
    if adb:
        def shell(*args):return _read_command([adb,'-s',device,'shell']+list(args))
        for command in (('dumpsys','activity','activities'),('dumpsys','window','windows')):
            text=shell(*command)
            for line in text.splitlines():
                if any(key in line for key in ('mResumedActivity','topResumedActivity','mCurrentFocus','mFocusedApp')):
                    hit=re.search(r'([A-Za-z][\w]*(?:\.[\w]+)+)/',line)
                    if hit and 'arknights' in hit[1].lower():foreground=hit[1];break
            if foreground:break
        for line in shell('pm','list','packages').splitlines():
            package=line.removeprefix('package:').strip()
            if re.fullmatch(r'[\w.]+',package) and 'arknights' in package.lower() and package not in packages:packages.append(package)
    configured=config.get('package_name','auto')
    if foreground:
        result['_package_candidates']=[foreground]
    elif packages:
        result['_package_candidates']=packages
    elif configured and configured!='auto':
        result['_package_candidates']=[configured]
    else:
        result['_package_candidates']=['com.hypergryph.arknights','com.hypergryph.arknights.bilibili']
    print('[自动检测] MuMu路径: '+root,flush=True)
    return result


def _install_promotion_priority(namespace):
    board = owned = None

    def copies_before_promotion(card):
        name = card['name']
        if not name:
            return 0
        count = board.owned.get(name)
        if count is not None:
            return count % 3
        matches = [count for label, count in board.owned.items()
                   if label and (label in name or name in label)]
        return matches[0] % 3 if len(matches) == 1 else 0

    def order_duplicates(cards):
        completing, building = [], []
        for card in cards:
            copies = owned(card)
            if copies == 2:
                completing.append(card)
            elif copies == 1:
                building.append(card)
        return completing + building

    # The v1.2 release freezes prep_phase in the EXE. Replace only its two
    # duplicate-selection functions, using the same extension hook as the SDK.
    prepare = namespace['prep_phase']
    code = prepare.__code__
    replacements = {
        ('owned', ('board',)): copies_before_promotion.__code__,
        ('<listcomp>', ('owned',)): order_duplicates.__code__,
    }
    constants = tuple(
        replacements.get((value.co_name, value.co_freevars), value)
        if isinstance(value, type(code)) and value.co_argcount == 1 else value
        for value in code.co_consts
    )
    if sum(before is not after for before, after in zip(code.co_consts, constants)) != len(replacements):
        print('[商店] 当前版本的同名干员排序无法更新，沿用原逻辑', flush=True)
        return
    prepare.__code__ = code.replace(co_consts=constants)


def _install_selection_rules(namespace):
    exec(compile("def passed_count(panel):\n    import re\n    import unicodedata\n    texts=[str(t) for _,y,t in panel if y<350]\n    for i,text in enumerate(texts):\n        if '通过' not in text:\n            continue\n        def normalize(value):\n            return ''.join(str(unicodedata.digit(c)) if c.isdigit() else c for c in value)\n        combined=normalize(text)\n        if i+1<len(texts):\n            following=normalize(texts[i+1]).strip()\n            if re.fullmatch(r'\\d+\\s*次?',following):\n                combined+=following\n        match=re.search(r'通过\\s*(?:模拟)?\\s*(\\d+)\\s*次',combined)\n        if match:\n            return int(match.group(1))\n        return None\n    return 0\n\n\ndef pick_strategy(done):\n    tap(1500,580,1.0)\n    rows=list(GRID_Y)\n    for page in range(8):\n        img=shot()\n        for gy in rows:\n            if not GRID_TOP+60<=gy<=GRID_BOTTOM-40:\n                continue\n            for gx in GRID_X:\n                tap(gx,gy,0.7)\n                count=None\n                name=''\n                for attempt in range(3):\n                    panel=ocr(shot(),(1440,150,1920,800))\n                    # A real strategy panel must be open before absence of a\n                    # completion banner can be interpreted as zero clears.\n                    if any('初始生命值' in str(t).replace(' ','') for _,_,t in panel):\n                        count=passed_count(panel)\n                        name=strategy_name(panel)\n                        if count is not None:\n                            break\n                    if attempt<2:\n                        time.sleep(0.6)\n                if count is None:\n                    log('通关次数读取失败，跳过该格',(gx,gy))\n                    continue\n                if not name:\n                    log('无法识别名称',(gx,gy),'已通过',count,'次')\n                label=name or '无法识别名称[%s,%s]'%(gx,gy)\n                if count>=TARGET:\n                    log('跳过策略:',label,'已通过',count,'次，目标',TARGET,'次')\n                    continue\n                log('选择策略: %s（已通过 %s 次，目标 %s 次）'%(label,count,TARGET))\n                return label\n        scroll_grid()\n        after=shot()\n        shift=grid_shift(img,after)\n        if shift is not None and abs(shift)<20:\n            break\n        rows=detect_rows(after)\n        if not rows:\n            break\n        log('策略列表下滑，继续查找（本页 %s 行）'%len(rows))\n    log('没有找到通关次数未达标的策略')\n    return None\n", 'selection_rules.py', 'exec'), namespace)
    original_save = namespace.get('save_done')
    if original_save:
        def save_known_names(done):
            return original_save({name for name in done if not str(name).startswith('无法识别名称[')})
        namespace['save_done'] = save_known_names
