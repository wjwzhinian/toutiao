"""验证 Prometheus 指标格式与标签转义。"""

from utils.metrics import Metrics


def test_metrics_render_prometheus_text_and_escape_labels():
    metrics = Metrics()
    metrics.inc("news_http_requests_total", method="GET", path="/news", status="200")
    metrics.inc("news_http_requests_total", method="GET", path="/news", status="200")
    metrics.inc("news_agent_runs_total", status='failed"\nnext')

    output = metrics.render()

    assert 'news_http_requests_total{method="GET",path="/news",status="200"} 2' in output
    assert 'status="failed\\"\\nnext"' in output
