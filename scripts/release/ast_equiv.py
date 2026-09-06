"""Witness functional equivalence of two wheels: every .py member parses to the same AST
after docstrings are dropped (comments never reach the AST). Reports any member whose
code differs, and members present in only one wheel."""
import sys, zipfile, ast
a=zipfile.ZipFile(sys.argv[1]); b=zipfile.ZipFile(sys.argv[2])
def strip_doc(tree):
    for node in ast.walk(tree):
        if isinstance(node,(ast.Module,ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and node.body:
            first=node.body[0]
            if isinstance(first,ast.Expr) and isinstance(getattr(first,"value",None),ast.Constant) and isinstance(first.value.value,str):
                node.body=node.body[1:] or [ast.Pass()]
    return tree
an=set(a.namelist()); bn=set(b.namelist())
pys=sorted(n for n in an&bn if n.endswith(".py"))
code_diff=[]; text_diff=0
for n in pys:
    sa=a.read(n); sb=b.read(n)
    if sa==sb: continue
    text_diff+=1
    da=ast.dump(strip_doc(ast.parse(sa))); db=ast.dump(strip_doc(ast.parse(sb)))
    if da!=db: code_diff.append(n)
print("py members compared:", len(pys), "| text-different:", text_diff, "| CODE-different:", len(code_diff))
for n in code_diff: print("  CODE DIFF:", n)
print("only in A:", sorted(an-bn)); print("only in B:", sorted(bn-an))
non_py=[n for n in sorted(an&bn) if not n.endswith(".py") and not n.startswith("systemu-") and a.read(n)!=b.read(n)]
print("non-py members differing:", non_py)
print("VERDICT:", "EQUIVALENT" if not code_diff else "NOT EQUIVALENT")
