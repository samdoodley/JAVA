class DashboardCharts {
  constructor() {
    this.charts = {};
    this.history = {
      equity: [],
      profit: [],
      deployment: [],
      profitable: [],
      losing: [],
    };
  }

  init() {
    this.charts.equity = this.createChart('equityChart', 'line', 'Today's Equity');
    this.charts.profit = this.createChart('profitChart', 'line', 'Profit Curve');
    this.charts.deployment = this.createChart('deploymentChart', 'bar', 'Capital Deployment');
    this.charts.profitable = this.createChart('profitableChart', 'bar', 'Top Profitable Stocks');
    this.charts.losing = this.createChart('losingChart', 'bar', 'Top Losing Stocks');
  }

  createChart(canvasId, type, label) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return null;
    const ctx = canvas.getContext('2d');
    return new Chart(ctx, {
      type,
      data: { labels: [], datasets: [] },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: false, ticks: { color: '#8fa0c2' } },
          x: { ticks: { color: '#8fa0c2' } },
        },
      },
    });
  }

  render(payload) {
    const report = payload?.report || {};
    const summary = payload?.summary || {};
    const label = report.timestamp || new Date().toLocaleTimeString();

    this.pushHistory('equity', summary.equity || 0, label);
    this.pushHistory('profit', summary.pnl_today || 0, label);
    this.pushHistory('deployment', summary.deployed || 0, label);

    this.renderSeries(this.charts.equity, this.history.equity, '#56d7ff', 'rgba(86, 215, 255, 0.2)', true);
    this.renderSeries(this.charts.profit, this.history.profit, '#3ddc97', 'rgba(61, 220, 151, 0.2)', true);
    this.renderSeries(this.charts.deployment, this.history.deployment, '#ffb84d', 'rgba(255, 184, 77, 0.2)', false);

    const profitable = (summary.top_profitable || []).slice(0, 5);
    this.charts.profitable?.data.labels.splice(0, this.charts.profitable.data.labels.length, ...profitable.map((item) => item.symbol));
    this.charts.profitable?.data.datasets.splice(0, this.charts.profitable.data.datasets.length, {
      data: profitable.map((item) => item.value || 0),
      backgroundColor: '#3ddc97',
    });
    this.charts.profitable?.update();

    const losing = (summary.top_losing || []).slice(0, 5);
    this.charts.losing?.data.labels.splice(0, this.charts.losing.data.labels.length, ...losing.map((item) => item.symbol));
    this.charts.losing?.data.datasets.splice(0, this.charts.losing.data.datasets.length, {
      data: losing.map((item) => item.value || 0),
      backgroundColor: '#ff6b6b',
    });
    this.charts.losing?.update();
  }

  pushHistory(key, value, label) {
    const series = this.history[key];
    series.push({ label, value });
    if (series.length > 20) {
      series.shift();
    }
  }

  renderSeries(chart, series, borderColor, fillColor, fill) {
    if (!chart) return;
    chart.data.labels = series.map((item) => item.label);
    chart.data.datasets = [{
      data: series.map((item) => item.value),
      borderColor,
      backgroundColor: fillColor,
      tension: 0.3,
      fill,
      pointRadius: 3,
    }];
    chart.update();
  }
}

window.dashboardCharts = new DashboardCharts();
