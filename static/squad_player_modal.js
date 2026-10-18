(() => {
  const modal = document.getElementById('squadPlayerModal');
  if (!modal) return;
  const profiles = new Map(JSON.parse(document.getElementById('squadPlayerProfiles').textContent)
    .map(player => [String(player.id), player]));
  const field = name => modal.querySelector('#spm' + name);
  const setText = (name, value) => { field(name).textContent = value ?? '—'; };
  let previousFocus = null;
  let previousOverflow = '';
  let theme = 0;
  const kitColors = ['#0284c7', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6'];

  function money(value) {
    return new Intl.NumberFormat('vi-VN', {maximumFractionDigits: 0}).format(value);
  }

  function switchTab(key) {
    modal.querySelectorAll('.pm-tab-btn').forEach(button => {
      const active = button.dataset.tab === key;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    modal.querySelectorAll('.pm-tab-panel').forEach(panel => {
      const active = panel.id === 'spmp-' + key;
      panel.classList.toggle('active', active);
      panel.setAttribute('aria-hidden', String(!active));
    });
    modal.querySelector('.pm-content').scrollTop = 0;
  }

  function renderAttributes(name, attributes, category) {
    const list = field(name);
    list.replaceChildren();
    for (const attribute of attributes) {
      const row = document.createElement('div');
      row.className = 'pm-attr-row ' + (attribute.is_core ? 'is-core core-' + category : 'non-core');
      const label = document.createElement('span');
      label.className = 'pm-attr-name';
      label.textContent = attribute.name;
      const value = document.createElement('span');
      value.className = 'pm-attr-val';
      value.textContent = attribute.val;
      row.append(label, value);
      list.append(row);
    }
  }

  function renderStats(stats) {
    const table = field('StatsTableBody');
    table.replaceChildren();
    const rows = [
      ['Ra sân trong mùa', 'apps_season'], ['Xuất sắc nhất trận', 'motm'],
      ['Bàn thắng trong mùa', 'goals_season'], ['Kiến tạo trong mùa', 'assists_season'],
      ['Thẻ vàng trong mùa', 'yellows_season'], ['Phong độ', 'form'],
      ['Tổng bàn thắng', 'goals_total'], ['Tổng số trận', 'apps_total'],
    ];
    for (const [label, key] of rows) {
      const row = document.createElement('tr');
      for (const competition of ['career', 'league', 'cup']) {
        const cell = document.createElement('td');
        const title = document.createElement('b');
        title.textContent = label + ': ';
        cell.append(title, String(stats[competition][key]));
        row.append(cell);
      }
      table.append(row);
    }
  }

  function open(pid) {
    const player = profiles.get(String(pid));
    if (!player) return;
    previousFocus = document.activeElement;
    previousOverflow = document.body.style.overflow;
    setText('Name', player.name);
    setText('Ovr', player.overall);
    setText('Stars', player.stars_str);
    setText('Age', player.age);
    setText('Pos', player.position);
    setText('Skill', player.special_skill);
    setText('Flag', player.flag);
    setText('JerseyNum', player.shirt_number);
    setText('Weight', player.weight + ' kg');
    setText('Height', player.height + ' cm');
    setText('Foot', player.preferred_foot);
    setText('Value', '💵 ' + money(player.market_value));
    setText('Wage', '💵 ' + money(player.salary));
    setText('FormPattern', player.form_pattern);
    setText('DefAvg', player.def_avg);
    setText('AtkAvg', player.atk_avg);
    setText('PhyAvg', player.phy_avg);
    renderAttributes('DefList', player.defense, 'def');
    renderAttributes('AtkList', player.attack, 'atk');
    renderAttributes('PhyList', player.physical, 'phy');
    setText('RoleName', 'Vai trò: ' + player.role);
    setText('RoleDesc', 'Thể lực ' + player.condition + '% · Tinh thần ' + player.morale +
      '% · Kinh nghiệm ' + player.experience_matches + ' trận, ' + player.seasons_played + ' mùa');
    const traits = field('TraitsList');
    traits.replaceChildren();
    for (const trait of player.traits.length ? player.traits : ['Chưa có kỹ năng đặc biệt']) {
      const badge = document.createElement('span');
      badge.className = 'badge gold';
      badge.textContent = trait;
      traits.append(badge);
    }
    renderStats(player.career_stats);
    switchTab('overview');
    modal.style.display = 'flex';
    modal.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    field('CloseBtn').focus();
  }

  function close() {
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = previousOverflow;
    if (previousFocus?.isConnected) previousFocus.focus({preventScroll: true});
  }

  field('CloseBtn').addEventListener('click', close);
  field('JerseySwapBtn').addEventListener('click', () => {
    theme = (theme + 1) % kitColors.length;
    field('JerseyGraphic').style.background = kitColors[theme];
  });
  modal.addEventListener('click', event => {
    if (event.target === modal) close();
    const tab = event.target.closest('.pm-tab-btn');
    if (tab) switchTab(tab.dataset.tab);
  });
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-squad-profile]');
    if (!link) return;
    event.preventDefault();
    open(link.dataset.squadProfile);
  });
  document.addEventListener('keydown', event => {
    if (modal.style.display === 'none') return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    } else if (event.key === 'Tab') {
      const buttons = Array.from(modal.querySelectorAll('button')).filter(button => !button.disabled);
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  window.SquadPlayerModal = {open, close};
})();
