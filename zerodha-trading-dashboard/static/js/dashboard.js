class DashboardUI {
  constructor() {
    this.lastPayload = null;
    this.refreshInterval = null;
    this.baseUrl = '/api/dashboard';
  }

  init() {
    this.updateClock();
    setInterval(() => this.updateClock(), 1000);
    this.fetchDashboard();
    this.refreshInterval = setInterval(() => this.fetchDashboard(), 1000);
  }

  updateClock() {
    const now = new Date();
    const time = now.toLocaleTimeString();
    const date = now.toLocaleDateString();
    document.getElementById('sidebar-time').textContent = time;
    document.getElementById('sidebar-date').textContent = date;
    document.getElementById('topbar-time').textContent = time;
    document.getElementById('topbar-date').textContent = date;
  }

  async fetchDashboard() {
    try {
      const response = await fetch(this.baseUrl, { cache: 'no-store' });
      if (!response.ok) {
        throw new Error(`Request failed: ${response.status}`);
      }
      const payload = await response.json();
      this.render(payload);
    } catch (error) {
      console.error('Dashboard refresh failed:', error);
      this.renderFallback();
    }
  }

  render(payload) {
    const report = payload?.report || {};
    const summary = payload?.summary || {};
    const status = payload?.status || {};
    const positions = payload?.positions || [];

    this.lastPayload = payload;
    this.setText('summary-pnl', this.formatCurrency(summary.pnl_today));
    this.setText('summary-realized', this.formatCurrency(summary.realized_pnl));
    this.setText('summary-equity', this.formatCurrency(summary.equity));
    this.setText('summary-deployed', this.formatCurrency(summary.deployed));
    this.setText('summary-win-rate', `${summary.win_rate ?? 0}%`);
    this.setText('summary-wins-losses', `${summary.wins ?? 0} / ${summary.losses ?? 0}`);
    this.setText('summary-trades', summary.total_trades ?? 0);
    this.setText('summary-open-positions', summary.open_positions ?? 0);
    this.setText('summary-symbols', summary.unique_symbols ?? 0);
    this.setText('last-update', report.timestamp || 'No update yet');

    const botStatus = report.status === 'Live' ? 'Bot Online' : 'Trading Bot Offline';
    const dataStatus = report.status === 'Live' ? 'Live Data' : 'Waiting for valid trading data';
    this.setText('bot-status-pill', botStatus);
    this.setText('data-source-pill', dataStatus);
    document.getElementById('bot-status-pill').className = `stat-pill ${report.status === 'Live' ? '' : 'muted'}`;
    document.getElementById('data-source-pill').className = `stat-pill ${report.status === 'Live' ? '' : 'muted'}`;

    const tbody = document.getElementById('positions-body');
    if (!tbody) return;
    if (!positions.length) {
      tbody.innerHTML = '<tr><td colspan="9">No open positions available.</td></tr>';
      document.getElementById('positions-count').textContent = '0 rows';
      return;
    }

    tbody.innerHTML = positions.map((row) => `
      <tr>
        <td>${row.symbol}</td>
        <td>${row.open_qty}</td>
        <td>${this.formatCurrency(row.capital_deployed)}</td>
        <td>${this.formatCurrency(row.unrealized_pnl)}</td>
        <td>${row.closed_qty}</td>
        <td>${this.formatCurrency(row.closed_pnl)}</td>
        <td>${row.buy_trades}</td>
        <td>${row.sell_trades}</td>
        <td>${row.open_position_count}</td>
      </tr>
    `).join('');
    document.getElementById('positions-count').textContent = `${positions.length} rows`;

    if (window.dashboardCharts) {
      window.dashboardCharts.render(payload);
    }
  }

  renderFallback() {
    this.setText('summary-pnl', '0.00');
    this.setText('summary-realized', '0.00');
    this.setText('summary-equity', '0.00');
    this.setText('summary-deployed', '0.00');
    this.setText('summary-win-rate', '0%');
    this.setText('summary-wins-losses', '0 / 0');
    this.setText('summary-trades', '0');
    this.setText('summary-open-positions', '0');
    this.setText('summary-symbols', '0');
    this.setText('last-update', 'No update yet');
    this.setText('bot-status-pill', 'Trading Bot Offline');
    this.setText('data-source-pill', 'Waiting for valid trading data');
  }

  setText(id, value) {
    const element = document.getElementById(id);
    if (element) {
      element.textContent = value;
    }
  }

  formatCurrency(value) {
    const number = Number(value || 0);
    return new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(number);
  }
}

window.dashboardUI = new DashboardUI();
window.dashboardUI.init();
