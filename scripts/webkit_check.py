"""Render a page in WebKitGTK (same engine family as iOS Safari) at a phone width,
report any roster row whose content spills past its box, and save a screenshot."""
import json, sys
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, WebKit, GLib

url, width, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
JS = r"""
(function(){
  var rows=[...document.querySelectorAll('.resp-row')];
  var bad=rows.filter(function(r){
    var rb=r.getBoundingClientRect();
    return [...r.querySelectorAll('.badge, .name, .slot')].some(function(e){
      var b=e.getBoundingClientRect(); return b.right>rb.right-1 || e.scrollWidth>e.clientWidth+1; });
  }).map(function(r){ return r.dataset.name || r.textContent.trim().replace(/\s+/g,' ').slice(0,40); });
  var row=rows.find(function(r){return r.classList.contains('noreply')});
  var b=row && row.querySelector('.badge').getBoundingClientRect(), rb=row && row.getBoundingClientRect();
  return JSON.stringify({ua:navigator.userAgent.match(/AppleWebKit\/[\d.]+/)[0], vw:innerWidth,
    rows:rows.length, spilling:bad, firstNoReply:row && {rowRight:Math.round(rb.right), badgeRight:Math.round(b.right), badgeWidth:Math.round(b.width)}});
})()
"""

app = Gtk.Application(application_id="dev.calloutroster.webkitcheck")
def on_activate(a):
    win = Gtk.ApplicationWindow(application=a); win.set_default_size(width, 844)
    view = WebKit.WebView(); win.set_child(view); win.present()
    def done_js(v, res):
        print(v.evaluate_javascript_finish(res).to_string())
        def done_shot(v, r):
            v.get_snapshot_finish(r).save_to_png(out); print("saved", out); a.quit()
        v.get_snapshot(WebKit.SnapshotRegion.VISIBLE, WebKit.SnapshotOptions.NONE, None, done_shot)
    def on_load(v, event):
        if event == WebKit.LoadEvent.FINISHED:
            GLib.timeout_add(400, lambda: (v.evaluate_javascript("document.querySelector('#teams').scrollIntoView(); 1", -1, None, None, None, lambda *_: None),
                                           GLib.timeout_add(300, lambda: (v.evaluate_javascript(JS, -1, None, None, None, done_js), False)[1]), False)[2])
    view.connect("load-changed", on_load)
    view.load_uri(url)
app.connect("activate", on_activate)
app.run([])
