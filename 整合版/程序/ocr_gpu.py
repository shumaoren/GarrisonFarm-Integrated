"""DirectML adapter selection for the bundled OCR runtime."""

import ctypes as ct
from ctypes import wintypes as wt
import json
import os
from pathlib import Path


class AdapterDesc(ct.Structure):
    _fields_ = [
        ("description", ct.c_wchar * 128),
        ("vendor", wt.UINT), ("device", wt.UINT), ("subsystem", wt.UINT), ("revision", wt.UINT),
        ("video_memory", ct.c_size_t), ("system_memory", ct.c_size_t), ("shared_memory", ct.c_size_t),
        ("luid_low", wt.DWORD), ("luid_high", wt.LONG), ("flags", wt.UINT),
    ]


def _method(pointer, index, result, *args):
    vtable = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p)))[0]
    return ct.WINFUNCTYPE(result, ct.c_void_p, *args)(vtable[index])


def _check_status(status, operation):
    if status < 0:
        raise OSError(f"{operation} 失败（0x{status & 0xffffffff:08X}）")


def adapters():
    factory = ct.c_void_p()
    # IID_IDXGIFactory1, with the first three GUID fields in little-endian order.
    iid = (ct.c_ubyte * 16).from_buffer_copy(bytes.fromhex("78ae0a776ff2ba4da829253c83d1b387"))
    create = ct.WinDLL("dxgi").CreateDXGIFactory1
    create.argtypes = [ct.c_void_p, ct.POINTER(ct.c_void_p)]
    create.restype = ct.c_long
    _check_status(create(ct.byref(iid), ct.byref(factory)), "读取显卡")
    devices = []
    try:
        # IDXGIFactory1::EnumAdapters1 keeps the DXGI indices used by DirectML.
        enumerate_adapter = _method(factory, 12, ct.c_long, wt.UINT, ct.POINTER(ct.c_void_p))
        index = 0
        while True:
            adapter = ct.c_void_p()
            status = enumerate_adapter(factory, index, ct.byref(adapter))
            if status & 0xffffffff == 0x887A0002:  # DXGI_ERROR_NOT_FOUND
                break
            _check_status(status, "枚举显卡")
            try:
                desc = AdapterDesc()
                get_desc = _method(adapter, 10, ct.c_long, ct.POINTER(AdapterDesc))
                _check_status(get_desc(adapter, ct.byref(desc)), "读取显卡信息")
                if not desc.flags & 2:  # DXGI_ADAPTER_FLAG_SOFTWARE
                    hardware_id = f"{desc.vendor:04x}:{desc.device:04x}:{desc.subsystem:08x}:{desc.revision:02x}"
                    luid = f"{desc.luid_high & 0xffffffff:08x}:{desc.luid_low:08x}"
                    devices.append({"index": index, "name": desc.description, "hardware_id": hardware_id, "luid": luid})
            finally:
                _method(adapter, 2, wt.ULONG)(adapter)
            index += 1
    finally:
        _method(factory, 2, wt.ULONG)(factory)
    return devices


def selected_index(setting):
    if setting["mode"] == "cpu":
        return None
    if setting["mode"] == "default":
        return 0
    if setting["mode"] != "adapter":
        raise ValueError("GPU 设置无效，请重新选择")
    matches = [gpu for gpu in adapters() if gpu["hardware_id"] == setting["hardware_id"]]
    if len(matches) == 1:
        return matches[0]["index"]
    for gpu in matches:
        if gpu['luid'] == setting.get('luid'):
            return gpu["index"]
    for gpu in matches:
        if gpu["index"] == setting["index"]:
            return gpu["index"]
    raise ValueError("找不到保存的显卡，请在 GPU 设置中重新选择")


def install(namespace, setting, sessions=None):
    from rapidocr_onnxruntime.utils import infer_engine

    index = selected_index(setting)
    original_session = infer_engine.InferenceSession
    original_ocr = namespace["RapidOCR"]
    provider = "CPUExecutionProvider" if index is None else "DmlExecutionProvider"

    def open_session(model, sess_options=None, providers=None, **kwargs):
        requested = [provider]
        if index is not None:
            sess_options.enable_mem_pattern = False
            requested = [(provider, {"device_id": index}), "CPUExecutionProvider"]
        session = original_session(model, sess_options=sess_options, providers=requested, **kwargs)
        if session.get_providers()[0] != provider:
            raise RuntimeError(f"OCR 未能加载 {provider}（显卡编号 {index}）")
        session.disable_fallback()
        if sessions is not None:
            sessions.append(session)
        return session

    def open_ocr(**kwargs):
        for model in ("det", "cls", "rec"):
            kwargs[model + "_use_dml"] = index is not None
        return original_ocr(**kwargs)

    infer_engine.InferenceSession = open_session
    namespace["RapidOCR"] = open_ocr
    if index is None:
        print("[OCR] 使用 CPU", flush=True)
    else:
        print(f"[OCR] 使用 DirectML，DXGI 编号 {index}", flush=True)


def read_settings(directory):
    encoded = os.environ.get("GF_TEST_GPU_SETTING")
    if encoded:
        return json.loads(encoded)
    path = Path(directory) / "gpu_settings.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {"mode": "cpu"}


def read_image(namespace, image, valid):
    import time

    sessions = []
    setting = read_settings(namespace["HERE"])
    install(namespace, setting, sessions)
    roots = []
    maa = namespace["find_maa"]()
    if maa:
        roots.append(Path(maa) / "resource" / "PaddleOCR")
    roots.append(Path(namespace["FALLBACK"]) / "PaddleOCR")
    errors = []
    for root in roots:
        started = time.perf_counter()
        sessions.clear()
        try:
            engine = namespace["RapidOCR"](
                det_model_path=str(root / "det/inference.onnx"),
                rec_model_path=str(root / "rec/inference.onnx"),
                rec_keys_path=str(root / "rec/keys.txt"),
                use_angle_cls=False,
            )
            texts, _ = engine(image)
            if len(sessions) != 3 or not texts or not valid(texts):
                raise RuntimeError("样张识别结果不符合预期，或模型载入不完整")
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(str(exc))
            continue
        print(f"[OCR 测试通过] {root.parent.name} 模型检测、识别通过，耗时 {time.perf_counter() - started:.2f} 秒", flush=True)
        return engine
    raise RuntimeError("；".join(errors))


def test_ocr(namespace):
    cv2, np = namespace["cv2"], namespace["np"]
    img = np.full((240, 720, 3), 255, dtype=np.uint8)
    cv2.putText(img, "Garrison Farm 12345", (32, 128), cv2.FONT_HERSHEY_SIMPLEX, 1.55, (0, 0, 0), 3)
    read_image(namespace, img, lambda texts: any("12345" in str(row[1]).replace(" ", "") for row in texts))
