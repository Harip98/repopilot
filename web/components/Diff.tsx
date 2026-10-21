interface FileDiff {
  path: string;
  tag: string | null;
  lines: string[];
}

/** Split `git diff` output into files, keeping each file's hunks and lines. */
export function parseDiff(diff: string): FileDiff[] {
  const files: FileDiff[] = [];
  let current: FileDiff | null = null;
  let inHunk = false; // inside a hunk every line is content, even "--- x" or "+++ y"
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const match = / b\/(.+)$/.exec(line);
      current = { path: match ? match[1] : line.slice(11), tag: null, lines: [] };
      files.push(current);
      inHunk = false;
    } else if (!current) {
      continue;
    } else if (inHunk || line.startsWith("@@")) {
      inHunk = true;
      current.lines.push(line);
    } else if (line.startsWith("new file mode")) {
      current.tag = "new file";
    } else if (line.startsWith("deleted file mode")) {
      current.tag = "deleted";
    } else if (line.startsWith("rename from ")) {
      current.tag = `renamed from ${line.slice(12)}`;
    } else if (line.startsWith("index ") || line.startsWith("--- ") || line.startsWith("+++ ")
      || line.startsWith("similarity index") || line.startsWith("rename to ")) {
      continue;
    } else {
      current.lines.push(line);
    }
  }
  return files;
}

function lineClass(line: string): string {
  if (line.startsWith("@@")) return "line hunk";
  if (line.startsWith("+")) return "line add";
  if (line.startsWith("-")) return "line del";
  return "line";
}

export function Diff({ diff }: { diff: string }) {
  const files = parseDiff(diff);
  if (files.length === 0) return <p className="muted">No changes.</p>;
  return (
    <div>
      {files.map((file) => {
        const added = file.lines.filter((l) => l.startsWith("+")).length;
        const removed = file.lines.filter((l) => l.startsWith("-")).length;
        return (
          <figure className="diff" key={file.path} style={{ margin: 0 }}>
            <figcaption className="diff-file">
              <span>{file.path}</span>
              <span className="tag">
                {file.tag ? `${file.tag}, ` : ""}+{added} −{removed}
              </span>
            </figcaption>
            <pre aria-label={`Changes to ${file.path}`}>
              {file.lines.map((line, i) => (
                <code key={i} className={lineClass(line)}>{line || " "}</code>
              ))}
            </pre>
          </figure>
        );
      })}
    </div>
  );
}
