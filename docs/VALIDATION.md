# Validation

Run from this repository directory:

~~~powershell
node tools\test_stadium1_viewer_bone_math.js
py -3 -m unittest discover -s tools -p "test_*.py"
node --check tools\stadium1_viewer\viewer.js
node --check tools\stadium1_viewer\bone_math.js
~~~

Live validation requires external user-owned assets. Do not write captures or
extraction caches into the repository. Before publishing, review:

~~~powershell
git status --short
git diff --check
git diff --cached --stat
~~~

The viewer defaults to loopback and its health API intentionally omits absolute
asset paths.
