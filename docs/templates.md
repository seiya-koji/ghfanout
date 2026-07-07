# Templates

Files with the `.jinja` extension are rendered as [Jinja2](https://jinja.palletsprojects.com/) templates and distributed under the name with that extension stripped (e.g. `pom.xml.jinja` → `pom.xml`). This lets you embed repository-specific values — the repository name, a version number — into otherwise shared files.

## Why only `.jinja` files?

Files without the `.jinja` extension are copied as-is, byte for byte. Because rendering is strictly opt-in per file, there is no risk of syntax conflicts with files that contain GitHub Actions' `${{ }}` or Maven's `${}` — those files simply stay untouched unless you deliberately give them a `.jinja` extension.

## Available variables

| Variable | Description |
| --- | --- |
| `{{ repo }}` | Destination repository name (= overlay directory name) |
| `{{ org }}` | The `org` from `ghfanout.yaml` |
| `{{ values.xxx }}` | A value defined under `values:` in `manifest.yaml` (can be nested) |

`repo` and `org` are built-in and always available, even without defining any `values`.

## Example

```xml
<!-- base/java-service/pom.xml.jinja -->
<groupId>com.example</groupId>
<artifactId>{{ repo }}</artifactId>
<version>{{ values.version | default("0.1.0") }}</version>
```

With `values: {version: "1.2.3"}` in the manifest of the `user-service` overlay, this renders as:

```xml
<groupId>com.example</groupId>
<artifactId>user-service</artifactId>
<version>1.2.3</version>
```

Jinja2 control structures and filters such as `{% if %}` can also be used. Lines containing only a block tag do not leave a blank line behind in the output (`trim_blocks` / `lstrip_blocks` are enabled, unlike Jinja2's defaults). Values can differ per branch — see [Per-branch overrides](configuration.md#per-branch-overrides).

## Rules and error conditions

- Referencing an undefined variable causes `build` to fail with an error. For values you want to be intentionally optional, provide a fallback with the `default` filter: `{{ values.version | default("0.1.0") }}`
- Having both `pom.xml` and `pom.xml.jinja` at the same relative path results in an error, even when they come from different profiles — use one or the other
- `.jinja` files must be UTF-8 text; giving the `.jinja` extension to a file that cannot be decoded as UTF-8 (such as a binary) results in an error
- [`.ghfanoutignore`](configuration.md#ghfanoutignore) patterns match the source name **before** the extension is stripped — to exclude `pom.xml.jinja`, write `pom.xml.jinja` (or `*.jinja`), not `pom.xml`
- Rendering is performed by both `build` and `deploy`, and trailing newlines are preserved, so no unnecessary diff appears at the destination
