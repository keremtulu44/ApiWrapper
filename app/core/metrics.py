"""Bağımlılıksız, Prometheus metin formatında hafif metrik toplayıcı."""

from __future__ import annotations

import threading
from collections import defaultdict

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

LabelKey = tuple[tuple[str, str], ...]


def _labels_to_key(labels: dict[str, str] | None) -> LabelKey:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _render_labels(key: LabelKey, extra: tuple[str, str] | None = None) -> str:
    items = list(key)
    if extra:
        items.append(extra)
    if not items:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in items)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class MetricsRegistry:
    """Counter, Gauge ve Histogram destekleyen thread-safe kayıt defteri."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[LabelKey, float]] = defaultdict(dict)
        self._gauges: dict[str, dict[LabelKey, float]] = defaultdict(dict)
        self._hist_sum: dict[str, dict[LabelKey, float]] = defaultdict(dict)
        self._hist_count: dict[str, dict[LabelKey, int]] = defaultdict(dict)
        self._hist_buckets: dict[str, dict[LabelKey, list[int]]] = defaultdict(dict)
        self._help: dict[str, tuple[str, str]] = {}

    def describe(self, name: str, kind: str, help_text: str) -> None:
        with self._lock:
            self._help[name] = (kind, help_text)

    def inc(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        key = _labels_to_key(labels)
        with self._lock:
            series = self._counters[name]
            series[key] = series.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = _labels_to_key(labels)
        with self._lock:
            self._gauges[name][key] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = _labels_to_key(labels)
        with self._lock:
            buckets = self._hist_buckets[name].setdefault(key, [0] * len(LATENCY_BUCKETS))
            for idx, bound in enumerate(LATENCY_BUCKETS):
                if value <= bound:
                    buckets[idx] += 1
            self._hist_sum[name][key] = self._hist_sum[name].get(key, 0.0) + value
            self._hist_count[name][key] = self._hist_count[name].get(key, 0) + 1

    def counter_total(self, name: str) -> float:
        """Bir counter'ın tüm etiket serileri toplamını döndürür."""
        with self._lock:
            return float(sum(self._counters.get(name, {}).values()))

    def render(self) -> str:
        """Prometheus text exposition formatı."""
        lines: list[str] = []
        with self._lock:
            for name, series in sorted(self._counters.items()):
                lines.extend(self._header(name, "counter"))
                for key, value in sorted(series.items()):
                    lines.append(f"{name}{_render_labels(key)} {_fmt(value)}")
            for name, series in sorted(self._gauges.items()):
                lines.extend(self._header(name, "gauge"))
                for key, value in sorted(series.items()):
                    lines.append(f"{name}{_render_labels(key)} {_fmt(value)}")
            for name, series_b in sorted(self._hist_buckets.items()):
                lines.extend(self._header(name, "histogram"))
                for key, buckets in sorted(series_b.items()):
                    cumulative = 0
                    for idx, bound in enumerate(LATENCY_BUCKETS):
                        cumulative = buckets[idx]
                        label = _render_labels(key, ("le", _fmt(bound)))
                        lines.append(f"{name}_bucket{label} {cumulative}")
                    total = self._hist_count[name].get(key, 0)
                    lines.append(f"{name}_bucket{_render_labels(key, ('le', '+Inf'))} {total}")
                    lines.append(f"{name}_sum{_render_labels(key)} "
                                 f"{_fmt(self._hist_sum[name].get(key, 0.0))}")
                    lines.append(f"{name}_count{_render_labels(key)} {total}")
        return "\n".join(lines) + "\n"

    def _header(self, name: str, default_kind: str) -> list[str]:
        kind, help_text = self._help.get(name, (default_kind, name))
        return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._hist_sum.clear()
            self._hist_count.clear()
            self._hist_buckets.clear()


def _fmt(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(round(value, 6))


metrics = MetricsRegistry()

metrics.describe("apiwrapper_requests_total", "counter", "Toplam HTTP istek sayısı.")
metrics.describe("apiwrapper_request_duration_seconds", "histogram", "HTTP istek süresi.")
metrics.describe("apiwrapper_upstream_requests_total", "counter", "Upstream istek sayısı.")
metrics.describe("apiwrapper_upstream_errors_total", "counter", "Upstream hata sayısı.")
metrics.describe(
    "apiwrapper_upstream_quota_errors_total",
    "counter",
    "Upstream hesap kısıtlama (kota) tespit sayısı.",
)
metrics.describe("apiwrapper_time_to_first_token_seconds", "histogram", "İlk token gecikmesi.")
metrics.describe("apiwrapper_tokens_total", "counter", "İşlenen token sayısı.")
metrics.describe("apiwrapper_active_streams", "gauge", "Aktif stream sayısı.")
metrics.describe("apiwrapper_recaptcha_tokens_total", "counter", "Üretilen reCAPTCHA token sayısı.")
metrics.describe("apiwrapper_circuit_breaker_state", "gauge", "Devre kesici durumu (0/1/2).")
metrics.describe(
    "apiwrapper_sessions_rotated_total",
    "counter",
    "Eşik aşıldığı için açılan yeni upstream sohbet sayısı.",
)
metrics.describe(
    "apiwrapper_account_switches_total",
    "counter",
    "Hesap değişimi sayısı (reason=switch hesap geçişi, reason=quota kilit).",
)
metrics.describe(
    "apiwrapper_account_messages_in_window",
    "gauge",
    "Hesap başına kota penceresi içindeki mesaj sayısı.",
)
metrics.describe(
    "apiwrapper_account_cooldown",
    "gauge",
    "Hesap dinlenmede mi (1) değil mi (0).",
)
