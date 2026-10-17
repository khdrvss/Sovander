/*
 * md.html: Gander's md.js (MIT, see LICENSES.md), without the card for a WebView too old to
 * update. Sovander adds two things: every picture that is not inside the file as data: is a
 * placeholder, never fetched (Requirement 9.2), and a link out of the file is told to the app,
 * which asks the reader before anything opens (9.3).
 */
(function () {
  var OUTSIDE = /^(https?|mailto|tel):/i;

  /*
   * Runs in the document DOMPurify parses into, which loads nothing. A picture that kept its
   * src until it reached this page would be asked for the moment it arrived, and refused by the
   * policy only after the asking.
   */
  DOMPurify.addHook("afterSanitizeAttributes", function (node) {
    if (node.nodeName !== "IMG") return;
    var src = node.getAttribute("src") || "";
    if (/^data:image\//i.test(src)) return;
    node.removeAttribute("src");
    node.setAttribute("data-vw-missing", src);
  });

  /* [box, its own words, address] of each placeholder, to say again in another language */
  var made = [];

  function say(box, alt, address) {
    box.textContent = alt || vwSay("Remote image not loaded");
    if (address) box.title = vwSay("Remote image not loaded") + ": " + address;
  }

  window.vwOnWords = function () {
    for (var i = 0; i < made.length; i++) say(made[i][0], made[i][1], made[i][2]);
  };

  function placeholders(root) {
    var missing = root.querySelectorAll("img[data-vw-missing]");
    for (var i = 0; i < missing.length; i++) {
      var img = missing[i];
      var box = document.createElement("span");
      box.className = "vw-remote";
      box.setAttribute("role", "img");
      var alt = (img.getAttribute("alt") || "").trim();
      var address = img.getAttribute("data-vw-missing");
      say(box, alt, address);
      made.push([box, alt, address]);
      img.parentNode.replaceChild(box, img);
    }
  }

  var content = document.getElementById("content");

  content.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var href = (a.getAttribute("href") || "").trim();
    // A jump within the file is the engine's to make
    if (href.charAt(0) === "#") return;
    e.preventDefault();
    if (OUTSIDE.test(href)) vwPost("link " + href);
  });

  // A middle click would open the link in a window of its own
  content.addEventListener("auxclick", function (e) {
    if (e.target && e.target.closest && e.target.closest("a[href]")) e.preventDefault();
  });

  vwFetchDoc("text")
    .then(function (txt) {
      var html = marked.parse(txt, { gfm: true, breaks: false });
      var made = DOMPurify.sanitize(html, { RETURN_DOM_FRAGMENT: true, FORBID_ATTR: ["srcset", "ping"] });
      placeholders(made);
      content.appendChild(made);
      vwStatusDone();
    })
    .catch(function (e) { vwFail("Could not render this Markdown file", e); });
})();
