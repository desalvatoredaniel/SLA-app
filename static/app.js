(function () {
  const page = document.body.dataset.page;

  function initServerHealth() {
    const viewport = document.getElementById('network-viewport');
    const board = document.getElementById('network-board');
    const signalLayer = document.getElementById('signal-layer');
    const zoomInButton = viewport ? viewport.querySelector('[data-zoom-in]') : null;
    const zoomOutButton = viewport ? viewport.querySelector('[data-zoom-out]') : null;
    const zoomResetButton = viewport ? viewport.querySelector('[data-zoom-reset]') : null;
    const zoomLabel = viewport ? viewport.querySelector('[data-zoom-label]') : null;

    if (!viewport || !board || !signalLayer) {
      return;
    }

    let offsetX = Number(board.dataset.offsetX || 0);
    let offsetY = Number(board.dataset.offsetY || 0);
    let scale = Number(board.dataset.scale || 1);
    const minScale = 0.55;
    const maxScale = 1.9;
    let dragging = false;
    let pointerStartX = 0;
    let pointerStartY = 0;
    const serverNodes = new Map(
      Array.from(board.querySelectorAll('[data-server]')).map((node) => [node.dataset.serverId, node]),
    );
    const serverRows = new Map(
      Array.from(document.querySelectorAll('[data-server-row]')).map((row) => [row.dataset.serverId, row]),
    );
    const signalLines = new Map();
    const lastCheckedByNode = new Map();
    const statusClasses = ['healthy', 'warning', 'critical'];

    function clampOffsets() {
      const viewportWidth = viewport.clientWidth || 0;
      const viewportHeight = viewport.clientHeight || 0;
      const boardWidth = Number(board.dataset.boardWidth || board.offsetWidth || 0);
      const boardHeight = Number(board.dataset.boardHeight || board.offsetHeight || 0);
      const scaledWidth = boardWidth * scale;
      const scaledHeight = boardHeight * scale;

      if (scaledWidth <= viewportWidth) {
        offsetX = (viewportWidth - scaledWidth) / 2;
      } else {
        const minX = viewportWidth - scaledWidth;
        offsetX = Math.max(minX, Math.min(0, offsetX));
      }

      if (scaledHeight <= viewportHeight) {
        offsetY = (viewportHeight - scaledHeight) / 2;
      } else {
        const minY = viewportHeight - scaledHeight;
        offsetY = Math.max(minY, Math.min(0, offsetY));
      }
    }

    function updateZoomLabel() {
      if (!zoomLabel) {
        return;
      }
      zoomLabel.textContent = `${Math.round(scale * 100)}%`;
    }

    function paintBoard() {
      clampOffsets();
      board.style.transform = `translate(${offsetX}px, ${offsetY}px) scale(${scale})`;
      updateZoomLabel();
    }

    function zoomAt(clientX, clientY, requestedScale) {
      const rect = viewport.getBoundingClientRect();
      const targetScale = Math.max(minScale, Math.min(maxScale, requestedScale));
      if (Math.abs(targetScale - scale) < 0.001) {
        return;
      }

      const pointerX = clientX - rect.left;
      const pointerY = clientY - rect.top;
      const boardX = (pointerX - offsetX) / scale;
      const boardY = (pointerY - offsetY) / scale;

      scale = targetScale;
      offsetX = pointerX - boardX * scale;
      offsetY = pointerY - boardY * scale;
      paintBoard();
    }

    function zoomByStep(direction) {
      const rect = viewport.getBoundingClientRect();
      const centerX = rect.left + rect.width / 2;
      const centerY = rect.top + rect.height / 2;
      const step = direction > 0 ? 1.12 : 1 / 1.12;
      zoomAt(centerX, centerY, scale * step);
    }

    function formatLastPing(isoValue) {
      if (!isoValue) {
        return 'never';
      }
      const date = new Date(isoValue);
      if (Number.isNaN(date.getTime())) {
        return isoValue;
      }
      return `${date.toLocaleDateString()} ${date.toLocaleTimeString()}`;
    }

    function triggerPulse(line, color, duration, delay) {
      const safeDuration = Math.max(0.45, Number(duration || 1.2));
      const safeDelay = Math.max(0, Number(delay || 0));
      const pulseColor = color || '#22d3ee';

      const pulse = document.createElement('div');
      pulse.className = 'signal-pulse is-live';
      pulse.style.setProperty('--signal-delay', `${safeDelay}s`);
      pulse.style.setProperty('--signal-color', pulseColor);
      pulse.style.setProperty('--pulse-duration', `${safeDuration}s`);

      const pulseReturn = document.createElement('div');
      pulseReturn.className = 'signal-pulse-return is-live';
      pulseReturn.style.setProperty('--signal-delay', `${(safeDelay + safeDuration * 0.55).toFixed(2)}s`);
      pulseReturn.style.setProperty('--signal-color', pulseColor);
      pulseReturn.style.setProperty('--pulse-duration', `${Math.max(0.45, safeDuration * 0.9).toFixed(2)}s`);

      line.append(pulse, pulseReturn);

      const removePulse = () => {
        pulse.remove();
        pulseReturn.remove();
      };

      window.setTimeout(removePulse, (safeDelay + safeDuration + 0.9) * 1000);
    }

    function applyStatusClass(element, status) {
      if (!element) {
        return;
      }
      element.classList.remove(...statusClasses);
      if (statusClasses.includes(status)) {
        element.classList.add(status);
      }
    }

    function updateStats(stats) {
      if (!stats || typeof stats !== 'object') {
        return;
      }
      const keys = ['total', 'healthy', 'warning', 'critical'];
      keys.forEach((key) => {
        const target = document.querySelector(`[data-stat="${key}"]`);
        if (target && stats[key] !== undefined) {
          target.textContent = String(stats[key]);
        }
      });
    }

    function updateServerCard(server) {
      const serverId = String(server.id);
      const node = serverNodes.get(serverId);
      if (!node) {
        return;
      }

      const previousStatus = node.dataset.status;
      node.dataset.status = server.status;
      node.dataset.lastCheckedAt = server.last_ping_at || '';
      applyStatusClass(node, server.status);

      if (previousStatus !== server.status) {
        const statusIcon = node.querySelector('.server-icons i:last-child');
        if (statusIcon) {
          const iconMap = {
            healthy: 'check-circle-2',
            warning: 'clock-3',
            critical: 'alert-circle',
          };
          statusIcon.setAttribute('data-lucide', iconMap[server.status] || 'help-circle');
          if (window.lucide && typeof window.lucide.createIcons === 'function') {
            window.lucide.createIcons();
          }
        }
      }

      const responseNode = node.querySelector('[data-node-response]');
      if (responseNode) {
        responseNode.textContent = server.last_check ? `${server.response_time}ms` : 'n/a';
      }

      const uptimeNode = node.querySelector('[data-node-uptime]');
      if (uptimeNode) {
        uptimeNode.textContent = `${server.uptime}% uptime`;
      }

      const httpNode = node.querySelector('[data-node-http]');
      if (httpNode) {
        if (!server.is_enabled) {
          httpNode.textContent = 'disabled';
        } else if (server.http_status) {
          httpNode.textContent = `HTTP ${server.http_status}`;
        } else {
          httpNode.textContent = 'HTTP n/a';
        }
      }

      const lastPingNode = node.querySelector('[data-node-last-ping]');
      if (lastPingNode) {
        lastPingNode.textContent = `last ping: ${formatLastPing(server.last_ping_at)}`;
      }

      const nodeGroup = node.querySelector('[data-node-group]');
      if (nodeGroup) {
        nodeGroup.textContent = server.server_group || '';
      }

      const row = serverRows.get(serverId);
      if (!row) {
        return;
      }

      const dot = row.querySelector('[data-row-dot]');
      applyStatusClass(dot, server.status);

      const statusPill = row.querySelector('[data-row-status-pill]');
      if (statusPill) {
        statusPill.textContent = String(server.status || '').toUpperCase();
        applyStatusClass(statusPill, server.status);
      }

      const enabledPill = row.querySelector('[data-row-enabled-pill]');
      if (enabledPill) {
        if (server.is_enabled) {
          enabledPill.classList.add('hidden');
        } else {
          enabledPill.classList.remove('hidden');
        }
      }

      const rowResponse = row.querySelector('[data-row-response]');
      if (rowResponse) {
        rowResponse.textContent = server.last_check ? `${server.response_time}ms` : 'n/a';
      }

      const rowUptime = row.querySelector('[data-row-uptime]');
      if (rowUptime) {
        rowUptime.textContent = `${server.uptime}%`;
      }

      const rowLastPing = row.querySelector('[data-row-last-ping]');
      if (rowLastPing) {
        rowLastPing.textContent = `last ping: ${formatLastPing(server.last_ping_at)}`;
      }
    }

    paintBoard();

    viewport.addEventListener('pointerdown', (event) => {
      dragging = true;
      pointerStartX = event.clientX;
      pointerStartY = event.clientY;
      viewport.classList.add('dragging');
      viewport.setPointerCapture(event.pointerId);
    });

    viewport.addEventListener('pointerup', (event) => {
      dragging = false;
      viewport.classList.remove('dragging');
      viewport.releasePointerCapture(event.pointerId);
    });

    viewport.addEventListener('pointermove', (event) => {
      if (!dragging) {
        return;
      }
      const deltaX = event.clientX - pointerStartX;
      const deltaY = event.clientY - pointerStartY;
      pointerStartX = event.clientX;
      pointerStartY = event.clientY;

      offsetX += deltaX;
      offsetY += deltaY;
      paintBoard();
    });

    viewport.addEventListener(
      'wheel',
      (event) => {
        event.preventDefault();
        const factor = event.deltaY < 0 ? 1.08 : 1 / 1.08;
        zoomAt(event.clientX, event.clientY, scale * factor);
      },
      { passive: false },
    );

    if (zoomInButton) {
      zoomInButton.addEventListener('click', () => zoomByStep(1));
    }
    if (zoomOutButton) {
      zoomOutButton.addEventListener('click', () => zoomByStep(-1));
    }
    if (zoomResetButton) {
      zoomResetButton.addEventListener('click', () => {
        scale = 1;
        offsetX = Number(board.dataset.offsetX || 0);
        offsetY = Number(board.dataset.offsetY || 0);
        paintBoard();
      });
    }

    window.addEventListener('resize', paintBoard);

    const mainframe = board.querySelector('[data-mainframe]');
    if (mainframe) {
      const mainX = Number(mainframe.dataset.x || 0);
      const mainY = Number(mainframe.dataset.y || 0);
      const nodes = Array.from(serverNodes.values());

      nodes.forEach((node, index) => {
        const nodeId = String(node.dataset.serverId || '');
        const nodeX = Number(node.dataset.x || 0);
        const nodeY = Number(node.dataset.y || 0);
        const status = node.dataset.status;
        const pingActive = node.dataset.pingActive === '1';
        const pingDuration = Number(node.dataset.pingDuration || 1.2);
        const pingDelay = Number(node.dataset.pingDelay || index * 0.12);

        const dx = nodeX - mainX;
        const dy = nodeY - mainY;
        const distance = Math.sqrt(dx * dx + dy * dy);
        const angle = (Math.atan2(dy, dx) * 180) / Math.PI;

        const colorMap = {
          healthy: '#22d3ee',
          warning: '#facc15',
          critical: '#f87171',
        };
        const pulseColor = node.dataset.pingColor || colorMap[status] || '#22d3ee';

        const line = document.createElement('div');
        line.className = 'signal-line';
        line.style.left = `${mainX}px`;
        line.style.top = `${mainY}px`;
        line.style.width = `${distance}px`;
        line.style.transform = `rotate(${angle}deg)`;

        const beam = document.createElement('div');
        beam.className = 'signal-beam';

        line.append(beam);
        signalLayer.appendChild(line);
        signalLines.set(nodeId, line);
        lastCheckedByNode.set(nodeId, node.dataset.lastCheckedAt || '');

        if (pingActive) {
          triggerPulse(line, pulseColor, pingDuration, pingDelay);
        }
      });
    }

    async function pollServerHealth() {
      try {
        const response = await fetch('/api/server-health/live', {
          method: 'GET',
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) {
          return;
        }

        const payload = await response.json();
        const servers = Array.isArray(payload.servers) ? payload.servers : [];
        const topology = payload.topology && typeof payload.topology === 'object' ? payload.topology : null;
        const incomingIds = new Set(servers.map((server) => String(server.id)));
        const currentIds = Array.from(serverNodes.keys());

        const hasTopologyChange =
          incomingIds.size !== currentIds.length ||
          currentIds.some((id) => !incomingIds.has(id)) ||
          (topology && String(board.dataset.topologySignature || '') !== String(topology.signature || '')) ||
          (topology &&
            (Number(board.dataset.boardWidth || 0) !== Number(topology.board_width || 0) ||
              Number(board.dataset.boardHeight || 0) !== Number(topology.board_height || 0) ||
              Number(mainframe?.dataset.x || 0) !== Number(topology.mainframe_x || 0) ||
              Number(mainframe?.dataset.y || 0) !== Number(topology.mainframe_y || 0))) ||
          servers.some((server) => {
            const node = serverNodes.get(String(server.id));
            if (!node) {
              return true;
            }
            const expectedX = String(Number(server.x));
            const expectedY = String(Number(server.y));
            return node.dataset.x !== expectedX || node.dataset.y !== expectedY;
          });

        if (hasTopologyChange) {
          window.location.reload();
          return;
        }

        updateStats(payload.stats);

        servers.forEach((server) => {
          const serverId = String(server.id);
          const previousCheckedAt = lastCheckedByNode.get(serverId) || '';
          const currentCheckedAt = server.last_ping_at || '';
          const hasNewPing = Boolean(currentCheckedAt) && previousCheckedAt !== currentCheckedAt;

          updateServerCard(server);

          if (hasNewPing && server.is_enabled) {
            const line = signalLines.get(serverId);
            if (line) {
              triggerPulse(
                line,
                server.ping_color || '#22d3ee',
                Number(server.ping_duration_seconds || 1.2),
                Number(server.ping_delay_seconds || 0),
              );
            }
          }

          lastCheckedByNode.set(serverId, currentCheckedAt);
        });
      } catch (error) {
        // Keep UI responsive even if a poll fails.
      }
    }

    pollServerHealth();
    window.setInterval(pollServerHealth, 3000);
  }

  function initSlaPayments() {
    const buttons = Array.from(document.querySelectorAll('.js-reprocess'));
    if (!buttons.length) {
      return;
    }

    buttons.forEach((button) => {
      button.addEventListener('click', async () => {
        const paymentId = button.dataset.paymentId;
        if (!paymentId) {
          return;
        }

        button.disabled = true;

        try {
          const response = await fetch(`/api/payments/${paymentId}/reprocess`, { method: 'POST' });
          const body = await response.json();
          if (!response.ok || !body.ok) {
            throw new Error('Request failed');
          }

          const card = document.querySelector(`[data-payment-card][data-payment-id="${paymentId}"]`);
          if (!card) {
            return;
          }

          card.classList.remove('failed', 'pending');
          card.classList.add('processing');

          const statusBadge = card.querySelector('[data-status-badge]');
          if (statusBadge) {
            statusBadge.textContent = 'PROCESSING';
            statusBadge.className = 'status-pill processing';
          }

          const progress = card.querySelector('[data-progress]');
          if (progress) {
            progress.classList.remove('hidden');
          }

          button.remove();
        } catch (error) {
          button.disabled = false;
        }
      });
    });
  }

  function initServerHealthConfig() {
    const forms = Array.from(document.querySelectorAll('.js-health-check-form'));
    if (!forms.length) {
      return;
    }

    function applyAuthVisibility(form) {
      const authSelect = form.querySelector('[data-auth-select]');
      if (!authSelect) {
        return;
      }

      const authType = authSelect.value;
      const groups = Array.from(form.querySelectorAll('[data-auth-group]'));
      groups.forEach((field) => {
        const targetType = field.getAttribute('data-auth-group');
        if (targetType === authType) {
          field.classList.remove('hidden');
        } else {
          field.classList.add('hidden');
        }
      });
    }

    forms.forEach((form) => {
      const authSelect = form.querySelector('[data-auth-select]');
      if (!authSelect) {
        return;
      }

      applyAuthVisibility(form);
      authSelect.addEventListener('change', () => applyAuthVisibility(form));
    });
  }

  if (page === 'server-health') {
    initServerHealth();
  }

  if (page === 'sla-payments') {
    initSlaPayments();
  }

  if (page === 'config-server-health') {
    initServerHealthConfig();
  }

  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }
})();
