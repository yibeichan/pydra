from pydra import mark, Workflow


@mark.task
def identity(x, y):
    return x, y


wf = Workflow(name="myworkflow", input_spec=["x"], x=1)
wf.add(identity(name="a").split("x", x=wf.lzin.x))
wf.add(identity(name="b").split("x", x=wf.a.lzout.out))
wf.add(identity(name="c").split("x", x=wf.b.lzout.out))  # .split("x", )
wf.add(identity(name="d", x=wf.c.lzout.out).combine(["a.x"]))
wf.add(identity(name="e", x=wf.d.lzout.out))
wf.set_output(("out", wf.e.lzout.out))

wf.inputs.x = [[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]]]
wf.inputs.y = [[[-1, -2, -3], [-4, -5, -6]], [[-7, -8, -9], [-10, -11, -12]]]

result = wf(plugin="serial")
print(result.output.out)
