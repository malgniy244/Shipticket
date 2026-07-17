"""
Remove the inline Bulk Mode JS block from index.html and replace it with:
  <script src="/static/bulk_patch.js?v=PLACEHOLDER"></script>
The PLACEHOLDER will be replaced at server startup with the actual content hash.
"""
import re

INDEX = "webapp/static/index.html"

with open(INDEX, "r") as f:
    content = f.read()

# The inline block starts at the Bulk Mode comment and ends at the closing </script>
# We need to find the comment, then find the next </script> that closes the main script tag.
# The structure is:
#   checkAuth();
#   // ── Bulk Mode (Decision 19) ...
#   ... 274 lines of bulk JS ...
#   </script>
#   </body>

# Strategy: find the comment, then find the last </script> before </body>
bulk_start = content.find("// \u2500\u2500 Bulk Mode (Decision 19)")
if bulk_start == -1:
    print("ERROR: Bulk Mode comment not found in index.html")
    exit(1)

# Find the </script> that closes the main script block (after bulk_start)
script_close = content.find("</script>", bulk_start)
if script_close == -1:
    print("ERROR: </script> not found after bulk mode block")
    exit(1)

# The replacement: remove the bulk JS and close the main script, then add the external script tag
before = content[:bulk_start]
after = content[script_close + len("</script>"):]

new_content = (
    before.rstrip() + "\n"
    "</script>\n"
    '<script src="/static/bulk_patch.js?v=__BULK_JS_HASH__"></script>\n'
    + after
)

with open(INDEX, "w") as f:
    f.write(new_content)

print(f"Done. Removed {len(content) - len(new_content)} chars of inline bulk JS.")
print("index.html now references /static/bulk_patch.js?v=__BULK_JS_HASH__")
