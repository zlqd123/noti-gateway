/* dsh-notifier-gate — visual config page for `muteRules`.
 *
 * Registers `plugins.bundle.config` + `plugins.row.config` cards. Reads the
 * live workspace/session lists from the host's native client services and
 * renders a checkbox-based UI. Writes stay staged through SettingsFormModel
 * (Save / Discard / writable / revision fence preserved).
 *
 * muteRules value shape (unchanged host contract):
 *   key = workspace label (cwd basename) or session id
 *   value = true (mute) | false (allow) | [channel, ...] (whitelist)
 */

;(function () {

if (typeof window === 'undefined' || !window.__ModuleLoader__) return

window.__ModuleLoader__.load({
  id: 'dsh-notifier-gate',
  factory: function (require) {
    function safeRequire(spec) {
      try { return require(spec) } catch (error) { return {} }
    }
    var React = safeRequire('react')
    var primitives = safeRequire('@deepseek-ai/dsh-client-ui-primitives')
    var createElement = React.createElement
    var useState = React.useState
    var useEffect = React.useEffect
    var NS = 'dsh-notifier-gate'
    // workspace/session data is optional (read via ctx.get + try/catch), so no
    // hard inject — a load-order miss never parks the config panel.
    var CLIENT_INJECT = ['locale', 'slots']

    var LOCALES = {
      en: {
        'card.title': 'Notification mute rules',
        'card.desc': 'Mute notifications per project or session.',
        'page.title': 'Notification mute rules',
        'page.hint': 'Mute whole projects or individual sessions. A session rule overrides its project.',
        'page.unavailable': 'Configuration unavailable',
        'page.readOnly': 'Read only',
        'page.save': 'Save',
        'page.saving': 'Saving…',
        'page.saveFailed': 'Save failed',
        'page.overridden': 'Overridden',
        'page.reset': 'Reset',
        'grp.projects': 'Projects (workspaces)',
        'grp.sessions': 'Sessions',
        'act.mute': 'Mute',
        'act.allow': 'Allow',
        'act.default': 'Default',
        'act.whitelist': 'Only channels',
        'empty': 'No projects or sessions detected yet. They appear here once you use them.',
        'col.project': 'Project',
        'col.action': 'Action',
        'muted': 'muted',
        'noData': 'Loading project/session list…',
      },
      zh: {
        'card.title': '通知静音规则',
        'card.desc': '按项目或会话静音通知。',
        'page.title': '通知静音规则',
        'page.hint': '按项目整体静音，或对单个会话设置。会话规则优先于项目规则。',
        'page.unavailable': '配置不可用',
        'page.readOnly': '只读',
        'page.save': '保存',
        'page.saving': '保存中…',
        'page.saveFailed': '保存失败',
        'page.overridden': '被上层覆盖',
        'page.reset': '重置',
        'grp.projects': '项目（工作区）',
        'grp.sessions': '会话',
        'act.mute': '静音',
        'act.allow': '放行',
        'act.default': '默认',
        'act.whitelist': '仅渠道',
        'empty': '暂未检测到项目或会话，使用后会出现在这里。',
        'col.project': '项目',
        'col.action': '操作',
        'muted': '已静音',
        'noData': '正在加载项目/会话列表…',
      },
    }

    // ---- theme-aware inline styles (no primitive components: third-party safe) ----
    var S = {
      wrap: { display: 'flex', flexDirection: 'column', gap: 10, padding: '2px 0' },
      group: { display: 'flex', flexDirection: 'column', gap: 4 },
      title: { fontWeight: 600, fontSize: 12, opacity: .85, margin: '2px 0' },
      row: { display: 'flex', alignItems: 'center', gap: 8, padding: '4px 6px', borderRadius: 6, borderBottom: '1px solid var(--dsw-alias-line, rgba(128,128,128,.2))' },
      name: { flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13 },
      sub: { fontSize: 11, opacity: .6 },
      btnGroup: { display: 'inline-flex', borderRadius: 6, overflow: 'hidden', border: '1px solid var(--dsw-alias-line, rgba(128,128,128,.35))' },
      btn: { border: 0, background: 'transparent', color: 'inherit', cursor: 'pointer', padding: '3px 9px', fontSize: 12 },
      btnOn: { background: 'var(--dsw-alias-accent, #4c8bf5)', color: '#fff' },
      check: { cursor: 'pointer' },
      muted: { opacity: .55 },
      hint: { fontSize: 12, opacity: .7 },
    }

    function basename(path) {
      if (typeof path !== 'string' || path === '') return ''
      var parts = path.replace(/\\/g, '/').split('/').filter(function (p) { return p !== '' })
      return parts.length ? parts[parts.length - 1] : path
    }

    // ---- muteRules <-> text (staged field payload) ---------------------------
    function dictToText(value) {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
      var lines = []
      for (var key in value) {
        if (!Object.prototype.hasOwnProperty.call(value, key)) continue
        var v = value[key]; var text
        if (v === true || v === false) text = String(v)
        else if (Array.isArray(v)) text = '[' + v.join(', ') + ']'
        else continue
        lines.push(key + ': ' + text)
      }
      return lines.join('\n')
    }
    function parseRuleValue(raw) {
      var s = String(raw == null ? '' : raw).trim()
      if (s === 'true' || s === 'false') return s === 'true'
      if (/^\[.*\]$/.test(s)) {
        return s.slice(1, -1).split(',').map(function (p) { return p.trim() }).filter(Boolean)
      }
      return undefined
    }
    function textToDict(text) {
      if (typeof text !== 'string') return {}
      var out = {}
      text.split(/\r?\n/).forEach(function (line) {
        line = line.trim()
        if (!line || line.charAt(0) === '#') return
        var i = line.indexOf(':')
        if (i < 0) return
        var key = line.slice(0, i).trim()
        var v = parseRuleValue(line.slice(i + 1))
        if (key && v !== undefined) out[key] = v
      })
      return out
    }

    function muteRulesField() {
      return {
        field: 'muteRules',
        format: function (value) { return dictToText(value) },
        parse: function (text) { return { kind: 'set', value: textToDict(text) } },
      }
    }

    // ---- controller ----------------------------------------------------------
    function GatePageController(scope, t) {
      var form = new primitives.SettingsFormModel(scope, [muteRulesField()])
      var self = this
      var actions = form.actions()
      this.pageStore = form.bind(function () {
        var snapshot = form.shell()
        snapshot.muteRules = form.field('muteRules')
        return snapshot
      })
      this.inject = function () {
        return {
          hooks: { gatePage: self.pageStore },
          edit: actions.edit,
          resetField: actions.resetField,
          save: actions.save,
          discard: actions.discard,
        }
      }
      this.dispose = function () { form.dispose && form.dispose() }
    }

    // ---- Visual editor -------------------------------------------------------
    // subscribe to a native SnapshotStore (getSnapshot/subscribe) with React
    function useStore(store) {
      var snap = useState(function () { return store && store.getSnapshot ? store.getSnapshot() : null })
      var value = snap[0], set = snap[1]
      useEffect(function () {
        if (!store || typeof store.subscribe !== 'function') return
        var off = store.subscribe(function () { if (store.getSnapshot) set(store.getSnapshot()) })
        if (store.getSnapshot) set(store.getSnapshot())
        return off
      }, [store])
      return value
    }

    function ActionButtons(props) {
      var t = props.t, value = props.value, onChange = props.onChange, disabled = props.disabled
      var mode = value === true ? 'mute' : value === false ? 'allow' : Array.isArray(value) ? 'white' : 'default'
      var opts = [
        ['default', t('act.default')],
        ['mute', t('act.mute')],
        ['allow', t('act.allow')],
      ]
      return createElement('span', { style: S.btnGroup },
        opts.map(function (o) {
          var on = mode === o[0]
          return createElement('button', {
            key: o[0], type: 'button', disabled: disabled,
            style: Object.assign({}, S.btn, on ? S.btnOn : {}),
            onClick: function () {
              onChange(o[0] === 'mute' ? true : o[0] === 'allow' ? false : undefined)
            },
          }, o[1])
        })
      )
    }

    function VisualEditor(props) {
      var t = props.t
      var useGatePage = props.useGatePage
      if (typeof useGatePage !== 'function') {
        return createElement('p', { style: S.hint }, t('page.unavailable'))
      }
      var st = useGatePage(function (s) { return s }) || {}
      var field = st.muteRules || {}
      var disabled = st.writable === false || st.saving === true
      var current = textToDict(field.text || '')

      // read live workspace / session snapshots via hooks
      var wss = props.workspaces || null
      var ses = props.sessions || null
      // subscribe so the list refreshes when workspaces/sessions load or change
      var wSnap = useStore(wss && wss.list)
      var sSnap = useStore(ses && ses.list)

      function writeDict(dict) {
        props.edit('muteRules', dictToText(dict))
      }
      function setRule(key, value) {
        var next = Object.assign({}, current)
        if (value === undefined) delete next[key]
        else next[key] = value
        writeDict(next)
      }

      var items = []
      if (wSnap && Array.isArray(wSnap.items)) {
        wSnap.items.forEach(function (w) {
          var label = basename(w.path) || w.title
          items.push({ key: label, type: 'ws', title: w.title || label, sub: basename(w.path), sessionIds: w.sessionIds || [] })
        })
      }
      var sessionById = {}
      if (sSnap) {
        var ids = sSnap.ids || []
        var byId = sSnap.byId || {}
        ids.forEach(function (id) {
          var s = byId[id] || {}
          var title = s.displayTitle || s.title || id
          sessionById[id] = { title: title }
        })
      }

      if (items.length === 0) {
        return createElement('div', { style: S.wrap },
          createElement('p', { style: S.hint }, wSnap ? t('empty') : t('noData')))
      }

      var projectRows = items.map(function (w) {
        return createElement('div', { key: 'ws-' + w.key, style: Object.assign({}, S.row, current[w.key] === true ? S.muted : {}) },
          createElement('span', { style: S.name, title: w.sub },
            w.title, createElement('span', { style: S.sub }, '  (' + w.key + ')')),
          createElement(ActionButtons, { t: t, value: current[w.key], onChange: function (v) { setRule(w.key, v) }, disabled: disabled })
        )
      })

      var sessionRows = []
      items.forEach(function (w) {
        var sids = (w.sessionIds || []).slice()
        // most-recently-active first (sessions come in manual order)
        if (sSnap && sSnap.byId) {
          sids.sort(function (a, b) {
            var ta = (sSnap.byId[a] && sSnap.byId[a].updatedAt) || 0
            var tb = (sSnap.byId[b] && sSnap.byId[b].updatedAt) || 0
            return tb - ta
          })
        }
        sids.forEach(function (sid) {
          var meta = sessionById[sid] || {}
          var label = meta.title || sid
          sessionRows.push(createElement('div', { key: 's-' + sid, style: Object.assign({}, S.row, current[sid] === true ? S.muted : {}) },
            createElement('label', { style: Object.assign({}, S.name, { display: 'flex', alignItems: 'center', gap: 8 }) },
              createElement('input', {
                type: 'checkbox', style: S.check, disabled: disabled,
                checked: current[sid] === true,
                onChange: function (e) { setRule(sid, e.target.checked ? true : undefined) },
              }),
              label, createElement('span', { style: S.sub }, '  (' + w.key + ')')
            ),
            createElement(ActionButtons, { t: t, value: current[sid], onChange: function (v) { setRule(sid, v) }, disabled: disabled })
          ))
        })
      })

      return createElement('div', { style: S.wrap },
        createElement('div', { style: S.group },
          createElement('div', { style: S.title }, t('grp.projects')),
          projectRows
        ),
        createElement('div', { style: S.group },
          createElement('div', { style: S.title }, t('grp.sessions')),
          sessionRows.length ? sessionRows : createElement('p', { style: S.hint }, t('empty'))
        )
      )
    }

    function GatePageCard(props) {
      var t = props.t
      var useGatePage = props.useGatePage
      if (typeof useGatePage !== 'function') {
        return createElement('p', { style: S.hint }, t('page.unavailable') + ' (' + (props.__diag || 'no-scope') + ')')
      }
      var st = useGatePage(function (s) { return s }) || {}
      return createElement(primitives.SettingsForm, {
        labels: {
          unavailable: t('page.unavailable'), readOnly: t('page.readOnly'),
          saveFailed: t('page.saveFailed'), save: t('page.save'), saving: t('page.saving'),
        },
        state: st,
        onSave: props.save,
        onDiscard: props.discard,
      },
        createElement('p', { style: S.hint }, t('page.hint')),
        createElement(VisualEditor, {
          t: t, useGatePage: useGatePage,
          workspaces: props.workspaces, sessions: props.sessions,
          edit: props.edit,
        })
      )
    }

    function apply(ctx) {
      try {
        ctx.effect(function () { return ctx.locale.register(NS, { zh: LOCALES.zh, en: LOCALES.en }) }, 'dsh-notifier-gate: dictionaries')
      } catch (error) {}

      var controller = null, boundT = null
      try { if (ctx.locale && typeof ctx.locale.bind === 'function') boundT = ctx.locale.bind(NS) } catch (error) { boundT = null }
      function t(key) {
        var out = null
        try { if (boundT) out = boundT(key) } catch (error) {}
        if (typeof out === 'string' && out !== key) return out
        return LOCALES.zh[key] !== undefined ? LOCALES.zh[key] : key
      }
      // Resolve native data services lazily on each inject() call (they may load
      // after this plugin); degrade to an empty list when absent.
      function dataServices() {
        var workspaces = null, sessions = null
        try { workspaces = ctx.get('workspaces') || null } catch (error) { workspaces = null }
        try { sessions = ctx.get('sessions') || null } catch (error) { sessions = null }
        return { workspaces: workspaces, sessions: sessions }
      }

      function install() {
        if (controller !== null) return
        try {
          if (!primitives || typeof primitives.SettingsFormModel !== 'function'
            || typeof primitives.SettingsForm !== 'function') return
          controller = new GatePageController(scope, t)
          ctx.effect(function () { return function () { controller.dispose() } }, 'dsh-notifier-gate: plugins-page form')
          var registerCard = function (slotName, key) {
            ctx.slots.inject(slotName, function () {
              return ctx.slots.register({
                name: slotName, key: key, locale: NS,
                inject: function () {
                  if (controller === null) return {}
                  var face = controller.inject()
                  face.t = t
                  var data = dataServices()
                  face.workspaces = data.workspaces
                  face.sessions = data.sessions
                  return face
                },
              }, GatePageCard)
            })
          }
          registerCard('plugins.bundle.config', 'dsh-notifier-gate')
          registerCard('plugins.row.config', 'dsh-notifier-gate#dsh-notifier-gate')
        } catch (error) { console.warn('[notifier-gate] plugins page install failed', error) }
      }

      var scope = null
      try {
        ctx.inject(['configForms'], function (sctx) {
          try {
            if (sctx.configForms && typeof sctx.configForms.get === 'function') {
              scope = sctx.configForms.get(NS)
              install()
            }
          } catch (error) { console.warn('[notifier-gate] configForms.get failed', error) }
        })
      } catch (error) { console.warn('[notifier-gate] configForms inject failed', error) }
    }

    return { apply: apply, inject: CLIENT_INJECT }
  },
})

})()
