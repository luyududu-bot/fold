"""Non-secret preferences on disk; optional credentials in macOS Keychain."""
import ctypes as C
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from . import providers
from .core import CloseoutError, atomic_json, read_json


class Keychain:
    def __init__(self):
        if sys.platform != "darwin":
            raise CloseoutError("当前系统未接入钥匙串，可取消记住 Key，仅在本次运行使用")
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.sec = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        signatures = {
            "CFStringCreateWithCString": ([C.c_void_p, C.c_char_p, C.c_uint32], C.c_void_p),
            "CFDataCreate": ([C.c_void_p, C.c_void_p, C.c_long], C.c_void_p),
            "CFDictionaryCreate": ([C.c_void_p, C.POINTER(C.c_void_p), C.POINTER(C.c_void_p), C.c_long, C.c_void_p, C.c_void_p], C.c_void_p),
            "CFDataGetLength": ([C.c_void_p], C.c_long),
            "CFDataGetBytePtr": ([C.c_void_p], C.c_void_p),
            "CFRelease": ([C.c_void_p], None),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.cf, name); fn.argtypes, fn.restype = args, result
        self.sec.SecItemAdd.argtypes = [C.c_void_p, C.c_void_p]
        self.sec.SecItemAdd.restype = C.c_int32
        self.sec.SecItemUpdate.argtypes = [C.c_void_p, C.c_void_p]
        self.sec.SecItemUpdate.restype = C.c_int32
        self.sec.SecItemCopyMatching.argtypes = [C.c_void_p, C.c_void_p]
        self.sec.SecItemCopyMatching.restype = C.c_int32

    def constant(self, name):
        return C.c_void_p.in_dll(self.cf if name.startswith("kCF") else self.sec, name).value

    @contextmanager
    def dictionary(self, values):
        owned, keys, refs = [], [], []
        try:
            for key, value in values.items():
                keys.append(self.constant(key))
                if isinstance(value, bytes):
                    buffer = C.create_string_buffer(value)
                    ref = self.cf.CFDataCreate(None, buffer, len(value)); owned.append(ref)
                elif isinstance(value, str) and value.startswith("kSec"):
                    ref = self.constant(value)
                elif value is True:
                    ref = self.constant("kCFBooleanTrue")
                else:
                    ref = self.cf.CFStringCreateWithCString(None, str(value).encode(), 0x08000100)
                    owned.append(ref)
                refs.append(ref)
            key_callbacks = C.addressof(C.c_byte.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks"))
            value_callbacks = C.addressof(C.c_byte.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks"))
            dictionary = self.cf.CFDictionaryCreate(None, (C.c_void_p * len(keys))(*keys),
                                                    (C.c_void_p * len(refs))(*refs), len(keys),
                                                    key_callbacks, value_callbacks)
            if not dictionary:
                raise CloseoutError("无法访问本机钥匙串")
            try:
                yield dictionary
            finally:
                self.cf.CFRelease(dictionary)
        finally:
            for ref in owned:
                if ref:
                    self.cf.CFRelease(ref)

    def query(self, provider):
        return {"kSecClass": "kSecClassGenericPassword", "kSecAttrService": "project-closeout.api-key",
                "kSecAttrAccount": provider}

    def exists(self, provider):
        with self.dictionary(self.query(provider)) as query:
            return self.sec.SecItemCopyMatching(query, None) == 0

    def get(self, provider):
        values = dict(self.query(provider), kSecReturnData=True)
        result = C.c_void_p()
        with self.dictionary(values) as query:
            status = self.sec.SecItemCopyMatching(query, C.byref(result))
        if status == -25300:
            return ""
        if status != 0:
            raise CloseoutError("无法读取已保存的 Key，请检查本机钥匙串授权")
        try:
            return C.string_at(self.cf.CFDataGetBytePtr(result), self.cf.CFDataGetLength(result)).decode()
        finally:
            if result:
                self.cf.CFRelease(result)

    def put(self, provider, key):
        values = dict(self.query(provider), kSecValueData=key.encode())
        with self.dictionary(values) as attrs:
            status = self.sec.SecItemAdd(attrs, None)
        if status == -25299:
            with self.dictionary(self.query(provider)) as query, self.dictionary({"kSecValueData": key.encode()}) as attrs:
                status = self.sec.SecItemUpdate(query, attrs)
        if status != 0:
            raise CloseoutError("Key 未能保存到钥匙串，可取消记住 Key 后仅在本次运行使用")


class Preferences:
    def __init__(self, path=None, vault_factory=Keychain):
        self.path = Path(path) if path else Path(__file__).resolve().parents[2] / "analysis-settings.local.json"
        self.vault_factory = vault_factory
        self.session_keys = {}
        self.data = {"enabled": False, "provider": "deepseek", "model": "", "remember": False}
        if self.path.is_file() and not self.path.is_symlink():
            try:
                saved = read_json(self.path)
                if saved.get("provider") in providers.DEFAULTS:
                    self.data.update(provider=saved["provider"],
                                     model=saved.get("model", "") if isinstance(saved.get("model"), str) else "",
                                     enabled=saved.get("enabled") is True,
                                     remember=saved.get("remember") is True)
            except (ValueError, OSError, TypeError, AttributeError):
                pass

    def public(self):
        status = dict(self.data)
        provider = status["provider"]
        status["has_key"] = bool(self.session_keys.get(provider) or os.environ.get(providers.DEFAULTS[provider][1]))
        if status["remember"] and not status["has_key"]:
            try:
                status["has_key"] = self.vault_factory().exists(provider)
            except (CloseoutError, OSError):
                pass
        return status

    def save(self, data):
        enabled = data.get("enabled") is True
        provider, model = data.get("provider"), data.get("model", "")
        if provider not in providers.DEFAULTS or not isinstance(model, str):
            raise CloseoutError("请选择服务商与模型")
        remember = data.get("remember") is True
        key = data.get("key", "")
        if not isinstance(key, str) or len(key) > 16384 or "\n" in key or "\r" in key:
            raise CloseoutError("API Key 格式无效")
        key = key.strip()
        if enabled:
            providers.config(None, provider, model)
            available = bool(key or self.session_keys.get(provider) or os.environ.get(providers.DEFAULTS[provider][1]))
            if remember and not available:
                available = self.vault_factory().exists(provider)
            if not available:
                raise CloseoutError("首次启用时请在本地填写 Key")
        if key and remember:
            self.vault_factory().put(provider, key)
        if key:
            self.session_keys[provider] = key
        self.data = {"enabled": enabled, "provider": provider, "model": model.strip(), "remember": remember}
        atomic_json(self.path, self.data)
        return self.public()

    def connection(self):
        if not self.data["enabled"]:
            return None
        provider = self.data["provider"]
        settings = providers.config(None, provider, self.data["model"])
        key = self.session_keys.get(provider) or os.environ.get(settings["api_key_env"], "")
        if not key and self.data["remember"]:
            key = self.vault_factory().get(provider)
        if not key:
            raise CloseoutError("尚未配置可用的 API Key，本次先提供本地建议")
        self.session_keys[provider] = key
        return settings, key
