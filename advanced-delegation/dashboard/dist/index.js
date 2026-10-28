// The dashboard extension only supplies an API; register an empty hidden page.
(function () {
  var plugins = window.__HERMES_PLUGINS__
  if (plugins && typeof plugins.register === 'function') {
    plugins.register('advanced-delegation', function AdvancedDelegationPlaceholder() {
      return null
    })
  }
})()
