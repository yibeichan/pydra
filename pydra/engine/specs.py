"""Task I/O specifications."""
from pathlib import Path
import typing as ty
import inspect
import re
import os
from glob import glob
import attr
from fileformats.generic import (
    File,
    Directory,
)

import pydra
from .helpers_file import template_update_single
from ..utils.hash import hash_function
from ..utils.misc import add_exc_note


T = ty.TypeVar("T")


def attr_fields(spec, exclude_names=()):
    return [field for field in spec.__attrs_attrs__ if field.name not in exclude_names]


# These are special types that are checked for in the construction of input/output specs
# and special converters inserted into the attrs fields.
#
# Ideally Multi(In|Out)putObj would be a generic (see https://github.com/python/mypy/issues/3331)
# and then Multi(In|Out)putFile could be just Multi(In|Out)obj.
class MultiInputObj(list, ty.Generic[T]):
    pass


MultiInputFile = MultiInputObj[File]


# Since we can't create a NewType from a type union, we add a dummy type to the union
# so we can detect the MultiOutput in the input/output spec creation
class MultiOutputType:
    pass


MultiOutputObj = ty.Union[list, object, MultiOutputType]
MultiOutputFile = ty.Union[File, ty.List[File], MultiOutputType]


@attr.s(auto_attribs=True, kw_only=True)
class SpecInfo:
    """Base data structure for metadata of specifications."""

    name: str
    """A name for the specification."""
    fields: ty.List[ty.Tuple] = attr.ib(factory=list)
    """List of names of fields (can be inputs or outputs)."""
    bases: ty.Sequence[ty.Type["BaseSpec"]] = attr.ib(factory=tuple)
    """Keeps track of specification inheritance.
       Should be a tuple containing at least one BaseSpec """


@attr.s(auto_attribs=True, kw_only=True)
class BaseSpec:
    """The base dataclass specs for all inputs and outputs."""

    # def __attrs_post_init__(self):
    #     self.files_hash = {
    #         field.name: {}
    #         for field in attr_fields(
    #             self, exclude_names=("_graph_checksums", "bindings", "files_hash")
    #         )
    #         if field.metadata.get("output_file_template") is None
    #     }

    def collect_additional_outputs(self, inputs, output_dir, outputs):
        """Get additional outputs."""
        return {}

    @property
    def hash(self):
        """Compute a basic hash for any given set of fields."""
        inp_dict = {}
        for field in attr_fields(
            self, exclude_names=("_graph_checksums", "bindings", "files_hash")
        ):
            if field.metadata.get("output_file_template"):
                continue
            # removing values that are not set from hash calculation
            if getattr(self, field.name) is attr.NOTHING:
                continue
            if "container_path" in field.metadata:
                continue
            inp_dict[field.name] = getattr(self, field.name)
        inp_hash = hash_function(inp_dict)
        if hasattr(self, "_graph_checksums"):
            inp_hash = hash_function((inp_hash, self._graph_checksums))
        return inp_hash

    def retrieve_values(self, wf, state_index: ty.Optional[int] = None):
        """Get values contained by this spec."""
        temp_values = {}
        for field in attr_fields(self):
            value = getattr(self, field.name)
            if isinstance(value, LazyField):
                temp_values[field.name] = value.get_value(wf, state_index=state_index)
        for field, val in temp_values.items():
            setattr(self, field, val)

    def check_fields_input_spec(self):
        """
        Check fields from input spec based on the medatada.

        e.g., if xor, requires are fulfilled, if value provided when mandatory.

        """
        fields = attr_fields(self)

        for field in fields:
            field_is_mandatory = bool(field.metadata.get("mandatory"))
            field_is_unset = getattr(self, field.name) is attr.NOTHING

            if field_is_unset and not field_is_mandatory:
                continue

            # Collect alternative fields associated with this field.
            alternative_fields = {
                name: getattr(self, name) is not attr.NOTHING
                for name in field.metadata.get("xor", [])
                if name != field.name
            }
            alternatives_are_set = any(alternative_fields.values())

            # Raise error if no field in mandatory alternative group is set.
            if field_is_unset:
                if alternatives_are_set:
                    continue
                message = f"{field.name} is mandatory and unset."
                if alternative_fields:
                    raise AttributeError(
                        message[:-1]
                        + f", but no alternative provided by {list(alternative_fields)}."
                    )
                else:
                    raise AttributeError(message)

            # Raise error if multiple alternatives are set.
            elif alternatives_are_set:
                set_alternative_fields = [
                    name for name, is_set in alternative_fields.items() if is_set
                ]
                raise AttributeError(
                    f"{field.name} is mutually exclusive with {set_alternative_fields}"
                )

            # Collect required fields associated with this field.
            required_fields = {
                name: getattr(self, name) is not attr.NOTHING
                for name in field.metadata.get("requires", [])
                if name != field.name
            }

            # Raise error if any required field is unset.
            if not all(required_fields.values()):
                unset_required_fields = [
                    name for name, is_set in required_fields.items() if not is_set
                ]
                raise AttributeError(f"{field.name} requires {unset_required_fields}")

    def check_metadata(self):
        """Check contained metadata."""

    def template_update(self):
        """Update template."""

    def copyfile_input(self, output_dir):
        """Copy the file pointed by a :class:`File` input."""


@attr.s(auto_attribs=True, kw_only=True)
class Runtime:
    """Represent run time metadata."""

    rss_peak_gb: ty.Optional[float] = None
    """Peak in consumption of physical RAM."""
    vms_peak_gb: ty.Optional[float] = None
    """Peak in consumption of virtual memory."""
    cpu_peak_percent: ty.Optional[float] = None
    """Peak in cpu consumption."""


@attr.s(auto_attribs=True, kw_only=True)
class Result:
    """Metadata regarding the outputs of processing."""

    output: ty.Optional[ty.Any] = None
    runtime: ty.Optional[Runtime] = None
    errored: bool = False

    def __getstate__(self):
        state = self.__dict__.copy()
        if state["output"] is not None:
            fields = tuple((el.name, el.type) for el in attr_fields(state["output"]))
            state["output_spec"] = (state["output"].__class__.__name__, fields)
            state["output"] = attr.asdict(state["output"], recurse=False)
        return state

    def __setstate__(self, state):
        if "output_spec" in state:
            spec = list(state["output_spec"])
            del state["output_spec"]
            klass = attr.make_class(
                spec[0], {k: attr.ib(type=v) for k, v in list(spec[1])}
            )
            state["output"] = klass(**state["output"])
        self.__dict__.update(state)

    def get_output_field(self, field_name):
        """Used in get_values in Workflow

        Parameters
        ----------
        field_name : `str`
            Name of field in LazyField object
        """
        if field_name == "all_":
            return attr.asdict(self.output, recurse=False)
        else:
            return getattr(self.output, field_name)


@attr.s(auto_attribs=True, kw_only=True)
class RuntimeSpec:
    """
    Specification for a task.

    From CWL::

        InlineJavascriptRequirement
        SchemaDefRequirement
        DockerRequirement
        SoftwareRequirement
        InitialWorkDirRequirement
        EnvVarRequirement
        ShellCommandRequirement
        ResourceRequirement

        InlineScriptRequirement

    """

    outdir: ty.Optional[str] = None
    container: ty.Optional[str] = "shell"
    network: bool = False


@attr.s(auto_attribs=True, kw_only=True)
class FunctionSpec(BaseSpec):
    """Specification for a process invoked from a shell."""

    def check_metadata(self):
        """
        Check the metadata for fields in input_spec and fields.

        Also sets the default values when available and needed.

        """
        supported_keys = {
            "allowed_values",
            "copyfile",
            "help_string",
            "mandatory",
            # "readonly", #likely not needed
            # "output_field_name", #likely not needed
            # "output_file_template", #likely not needed
            "requires",
            "keep_extension",
            "xor",
            "sep",
        }
        for fld in attr_fields(self, exclude_names=("_func", "_graph_checksums")):
            mdata = fld.metadata
            # checking keys from metadata
            if set(mdata.keys()) - supported_keys:
                raise AttributeError(
                    f"only these keys are supported {supported_keys}, but "
                    f"{set(mdata.keys()) - supported_keys} provided"
                )
            # checking if the help string is provided (required field)
            if "help_string" not in mdata:
                raise AttributeError(f"{fld.name} doesn't have help_string field")
            # not allowing for default if the field is mandatory
            if not fld.default == attr.NOTHING and mdata.get("mandatory"):
                raise AttributeError(
                    "default value should not be set when the field is mandatory"
                )
            # setting default if value not provided and default is available
            if getattr(self, fld.name) is None:
                if not fld.default == attr.NOTHING:
                    setattr(self, fld.name, fld.default)


@attr.s(auto_attribs=True, kw_only=True)
class ShellSpec(BaseSpec):
    """Specification for a process invoked from a shell."""

    executable: ty.Union[str, ty.List[str]] = attr.ib(
        metadata={
            "help_string": "the first part of the command, can be a string, "
            "e.g. 'ls', or a list, e.g. ['ls', '-l', 'dirname']"
        }
    )
    args: ty.Union[str, ty.List[str], None] = attr.ib(
        None,
        metadata={
            "help_string": "the last part of the command, can be a string, "
            "e.g. <file_name>, or a list"
        },
    )

    def retrieve_values(self, wf, state_index=None):
        """Parse output results."""
        temp_values = {}
        for field in attr_fields(self):
            # retrieving values that do not have templates
            if not field.metadata.get("output_file_template"):
                value = getattr(self, field.name)
                if isinstance(value, LazyField):
                    temp_values[field.name] = value.get_value(
                        wf, state_index=state_index
                    )
        for field, val in temp_values.items():
            value = path_to_string(value)
            setattr(self, field, val)

    def check_metadata(self):
        """
        Check the metadata for fields in input_spec and fields.

        Also sets the default values when available and needed.

        """
        supported_keys = {
            "allowed_values",
            "argstr",
            "container_path",
            "copyfile",
            "help_string",
            "mandatory",
            "readonly",
            "output_field_name",
            "output_file_template",
            "position",
            "requires",
            "keep_extension",
            "xor",
            "sep",
            "formatter",
            "_output_type",
        }
        for fld in attr_fields(self, exclude_names=("_func", "_graph_checksums")):
            mdata = fld.metadata
            # checking keys from metadata
            if set(mdata.keys()) - supported_keys:
                raise AttributeError(
                    f"only these keys are supported {supported_keys}, but "
                    f"{set(mdata.keys()) - supported_keys} provided"
                )
            # checking if the help string is provided (required field)
            if "help_string" not in mdata:
                raise AttributeError(f"{fld.name} doesn't have help_string field")
            # assuming that fields with output_file_template shouldn't have default
            if fld.default not in [attr.NOTHING, True, False] and mdata.get(
                "output_file_template"
            ):
                raise AttributeError(
                    "default value should not be set together with output_file_template"
                )
            # not allowing for default if the field is mandatory
            if not fld.default == attr.NOTHING and mdata.get("mandatory"):
                raise AttributeError(
                    "default value should not be set when the field is mandatory"
                )
            # setting default if value not provided and default is available
            if getattr(self, fld.name) is None:
                if not fld.default == attr.NOTHING:
                    setattr(self, fld.name, fld.default)


@attr.s(auto_attribs=True, kw_only=True)
class ShellOutSpec:
    """Output specification of a generic shell process."""

    return_code: int
    """The process' exit code."""
    stdout: str
    """The process' standard output."""
    stderr: str
    """The process' standard input."""

    def collect_additional_outputs(self, inputs, output_dir, outputs):
        from ..utils.typing import TypeParser

        """Collect additional outputs from shelltask output_spec."""
        additional_out = {}
        for fld in attr_fields(self, exclude_names=("return_code", "stdout", "stderr")):
            if not TypeParser.is_subclass(
                fld.type,
                (
                    os.PathLike,
                    MultiOutputObj,
                    int,
                    float,
                    bool,
                    str,
                    list,
                ),
            ):
                raise TypeError(
                    f"Support for {fld.type} type, required for {fld.name} in {self}, "
                    "has not been implemented in collect_additional_output"
                )
            # assuming that field should have either default or metadata, but not both
            input_value = getattr(inputs, fld.name, attr.NOTHING)
            if input_value is not attr.NOTHING:
                if issubclass(fld.type, os.PathLike):
                    input_value = fld.type(input_value)
                additional_out[fld.name] = input_value
            elif (
                fld.default is None or fld.default == attr.NOTHING
            ) and not fld.metadata:  # TODO: is it right?
                raise AttributeError("File has to have default value or metadata")
            elif fld.default != attr.NOTHING:
                additional_out[fld.name] = self._field_defaultvalue(fld, output_dir)
            elif fld.metadata:
                if (
                    fld.type in [int, float, bool, str, list]
                    and "callable" not in fld.metadata
                ):
                    raise AttributeError(
                        f"{fld.type} has to have a callable in metadata"
                    )
                additional_out[fld.name] = self._field_metadata(
                    fld, inputs, output_dir, outputs
                )
        return additional_out

    def generated_output_names(self, inputs, output_dir):
        """Returns a list of all outputs that will be generated by the task.
        Takes into account the task input and the requires list for the output fields.
        TODO: should be in all Output specs?
        """
        # checking the input (if all mandatory fields are provided, etc.)
        inputs.check_fields_input_spec()
        output_names = ["return_code", "stdout", "stderr"]
        for fld in attr_fields(self, exclude_names=("return_code", "stdout", "stderr")):
            if fld.type not in [File, MultiOutputFile, Directory]:
                raise Exception("not implemented (collect_additional_output)")
            # assuming that field should have either default or metadata, but not both
            if (
                fld.default in (None, attr.NOTHING) and not fld.metadata
            ):  # TODO: is it right?
                raise AttributeError("File has to have default value or metadata")
            elif fld.default != attr.NOTHING:
                output_names.append(fld.name)
            elif (
                fld.metadata
                and self._field_metadata(
                    fld, inputs, output_dir, outputs=None, check_existance=False
                )
                != attr.NOTHING
            ):
                output_names.append(fld.name)
        return output_names

    def _field_defaultvalue(self, fld, output_dir):
        """Collect output file if the default value specified."""
        if not isinstance(fld.default, (str, Path)):
            raise AttributeError(
                f"{fld.name} is a File, so default value "
                f"should be a string or a Path, "
                f"{fld.default} provided"
            )
        default = fld.default
        if isinstance(default, str):
            default = Path(default)

        default = output_dir / default
        if "*" not in str(default):
            if default.exists():
                return default
            else:
                raise AttributeError(f"file {default} does not exist")
        else:
            all_files = [Path(el) for el in glob(str(default.expanduser()))]
            if len(all_files) > 1:
                return all_files
            elif len(all_files) == 1:
                return all_files[0]
            else:
                raise AttributeError(f"no file matches {default.name}")

    def _field_metadata(
        self, fld, inputs, output_dir, outputs=None, check_existance=True
    ):
        """Collect output file if metadata specified."""
        if self._check_requires(fld, inputs) is False:
            return attr.NOTHING

        if "value" in fld.metadata:
            return output_dir / fld.metadata["value"]
        # this block is only run if "output_file_template" is provided in output_spec
        # if the field is set in input_spec with output_file_template,
        # than the field already should have value
        elif "output_file_template" in fld.metadata:
            value = template_update_single(
                fld, inputs=inputs, output_dir=output_dir, spec_type="output"
            )

            if fld.type is MultiOutputFile and type(value) is list:
                # TODO: how to deal with mandatory list outputs
                ret = []
                for val in value:
                    val = Path(val)
                    if check_existance and not val.exists():
                        ret.append(attr.NOTHING)
                    else:
                        ret.append(val)
                return ret
            else:
                val = Path(value)
                # checking if the file exists
                if check_existance and not val.exists():
                    # if mandatory raise exception
                    if "mandatory" in fld.metadata:
                        if fld.metadata["mandatory"]:
                            raise Exception(
                                f"mandatory output for variable {fld.name} does not exist"
                            )
                    return attr.NOTHING
                return val
        elif "callable" in fld.metadata:
            callable_ = fld.metadata["callable"]
            if isinstance(callable_, staticmethod):
                # In case callable is defined as a static method,
                # retrieve the function wrapped in the descriptor.
                callable_ = callable_.__func__
            call_args = inspect.getfullargspec(callable_)
            call_args_val = {}
            for argnm in call_args.args:
                if argnm == "field":
                    call_args_val[argnm] = fld
                elif argnm == "output_dir":
                    call_args_val[argnm] = output_dir
                elif argnm == "inputs":
                    call_args_val[argnm] = inputs
                elif argnm == "stdout":
                    call_args_val[argnm] = outputs["stdout"]
                elif argnm == "stderr":
                    call_args_val[argnm] = outputs["stderr"]
                else:
                    try:
                        call_args_val[argnm] = getattr(inputs, argnm)
                    except AttributeError:
                        raise AttributeError(
                            f"arguments of the callable function from {fld.name} "
                            f"has to be in inputs or be field or output_dir, "
                            f"but {argnm} is used"
                        )
            return callable_(**call_args_val)
        else:
            raise Exception("(_field_metadata) is not a current valid metadata key.")

    def _check_requires(self, fld, inputs):
        """checking if all fields from the requires and template are set in the input
        if requires is a list of list, checking if at least one list has all elements set
        """
        from .helpers import ensure_list

        if "requires" in fld.metadata:
            # if requires is a list of list it is treated as el[0] OR el[1] OR...
            required_fields = ensure_list(fld.metadata["requires"])
            if all([isinstance(el, list) for el in required_fields]):
                field_required_OR = required_fields
            # if requires is a list of tuples/strings - I'm creating a 1-el nested list
            elif all([isinstance(el, (str, tuple)) for el in required_fields]):
                field_required_OR = [required_fields]
            else:
                raise Exception(
                    f"requires field can be a list of list, or a list "
                    f"of strings/tuples, but {fld.metadata['requires']} "
                    f"provided for {fld.name}"
                )
        else:
            field_required_OR = [[]]

        for field_required in field_required_OR:
            # if the output has output_file_template field,
            # adding all input fields from the template to requires
            if "output_file_template" in fld.metadata:
                template = fld.metadata["output_file_template"]
                # if a template is a function it has to be run first with the inputs as the only arg
                if callable(template):
                    template = template(inputs)
                inp_fields = re.findall(r"{\w+}", template)
                field_required += [
                    el[1:-1] for el in inp_fields if el[1:-1] not in field_required
                ]

        # it's a flag, of the field from the list is not in input it will be changed to False
        required_found = True
        for field_required in field_required_OR:
            required_found = True
            # checking if the input fields from requires have set values
            for inp in field_required:
                if isinstance(inp, str):  # name of the input field
                    if not hasattr(inputs, inp):
                        raise Exception(
                            f"{inp} is not a valid input field, can't be used in requires"
                        )
                    elif getattr(inputs, inp) in [attr.NOTHING, None]:
                        required_found = False
                        break
                elif isinstance(inp, tuple):  # (name, allowed values)
                    inp, allowed_val = inp[0], ensure_list(inp[1])
                    if not hasattr(inputs, inp):
                        raise Exception(
                            f"{inp} is not a valid input field, can't be used in requires"
                        )
                    elif getattr(inputs, inp) not in allowed_val:
                        required_found = False
                        break
                else:
                    raise Exception(
                        f"each element of the requires element should be a string or a tuple, "
                        f"but {inp} is found in {field_required}"
                    )
            # if the specific list from field_required_OR has all elements set, no need to check more
            if required_found:
                break

        if required_found:
            return True
        else:
            return False


@attr.s(auto_attribs=True, kw_only=True)
class ContainerSpec(ShellSpec):
    """Refine the generic command-line specification to container execution."""

    image: ty.Union[File, str] = attr.ib(
        metadata={"help_string": "image", "mandatory": True}
    )
    """The image to be containerized."""
    container: ty.Union[File, str, None] = attr.ib(
        metadata={"help_string": "container"}
    )
    """The container."""
    container_xargs: ty.Optional[ty.List[str]] = attr.ib(
        default=None, metadata={"help_string": "todo"}
    )


@attr.s(auto_attribs=True, kw_only=True)
class DockerSpec(ContainerSpec):
    """Particularize container specifications to the Docker engine."""

    container: str = attr.ib("docker", metadata={"help_string": "container"})


@attr.s(auto_attribs=True, kw_only=True)
class SingularitySpec(ContainerSpec):
    """Particularize container specifications to Singularity."""

    container: str = attr.ib("singularity", metadata={"help_string": "container type"})


@attr.s
class LazyInterface:
    _node: "core.TaskBase" = attr.ib()
    _attr_type: str

    def __getattr__(self, name):
        if name in ("_node", "_attr_type", "_field_names"):
            raise AttributeError(f"{name} hasn't been set yet")
        if name not in self._field_names:
            raise AttributeError(
                f"Task {self._node.name} has no {self._attr_type} attribute {name}"
            )

        type_ = self._get_type(name)
        task = self._node
        splits = task._splits
        combines = task._combines
        for combiner in combines:
            try:
                splits.remove(combiner)
            except KeyError:
                # For combinations referring to only one field in a nested splitter spec
                splitter = next(
                    s for s in splits if combiner in self._node._unwrap_splitter(s)
                )
                splits.remove(splitter)
        if combines:
            type_ = ty.List[type_]
        if splits:
            # for _ in splits:
            #     type_ = Split[type_]
            type_ = Split[type_]

        return LazyField[type_](
            name=self._node.name,
            field=name,
            attr_type=self._attr_type,
            type=type_,
            splits=splits,
        )


class LazyIn(LazyInterface):
    _attr_type = "input"

    def _get_type(self, name):
        attr = next(t for n, t in self._node.input_spec.fields if n == name)
        if attr is None:
            return ty.Any
        else:
            return attr.type

    @property
    def _field_names(self):
        return [field[0] for field in self._node.input_spec.fields]


class LazyOut(LazyInterface):
    _attr_type = "output"

    def _get_type(self, name):
        try:
            type_ = next(f[1] for f in self._node.output_spec.fields if f[0] == name)
        except StopIteration:
            type_ = ty.Any
        else:
            if not inspect.isclass(type_):
                try:
                    type_ = type_.type  # attrs _CountingAttribute
                except AttributeError:
                    pass  # typing._SpecialForm
        return type_

    @property
    def _field_names(self):
        return self._node.output_names + ["all_"]


TypeOrAny = ty.Union[ty.Type[ty.Any], ty.Any]
Splitter = ty.Union[str, ty.Tuple[str, ...]]


@attr.s(auto_attribs=True, kw_only=True)
class LazyField(ty.Generic[T]):
    """Lazy fields implement promises."""

    name: str
    field: str
    attr_type: str
    type: TypeOrAny
    splits: ty.Set[Splitter] = attr.field(factory=set)

    def __repr__(self):
        return f"LF('{self.name}', '{self.field}', {self.type})"

    def get_value(
        self, wf: "pydra.Workflow", state_index: ty.Optional[int] = None
    ) -> ty.Any:
        """Return the value of a lazy field.

        Parameters
        ----------
        wf : Workflow
            the workflow the lazy field references
        state_index : int, optional
            the state index of the field to access

        Returns
        -------
        value : Any
            the resolved value of the lazy-field
        """
        from ..utils.typing import TypeParser  # pylint: disable=import-outside-toplevel

        if self.attr_type == "input":
            value = getattr(wf.inputs, self.field)
            if TypeParser.is_subclass(self.type, Split) and not getattr(
                wf, "pre_split", False
            ):
                nested_splits, _ = TypeParser.nested_sequence_types(
                    self.type, only_splits=True
                )

                def apply_splits(obj, depth):
                    if depth < 1:
                        return obj
                    return Split(apply_splits(i, depth - 1) for i in obj)

                value = apply_splits(value, len(nested_splits))
        elif self.attr_type == "output":
            node = getattr(wf, self.name)
            result = node.result(state_index=state_index)
            nested_sequences, _ = TypeParser.nested_sequence_types(self.type)

            def get_nested_results(res, nested_seqs):
                if isinstance(res, list):
                    if not nested_seqs:
                        raise ValueError(
                            f"Declared type for field {self.name} in {self.name}, {self.type}, "
                            f"does not match the level of nested results returned {result}"
                        )
                    val = nested_seqs[0](
                        get_nested_results(res=r, nested_seqs=nested_seqs[1:])
                        for r in res
                    )
                else:
                    if res.errored:
                        raise ValueError(
                            f"Cannot retrieve value for {self.field} from {self.name} as "
                            "the node errored"
                        )
                    val = res.get_output_field(self.field)
                    if nested_seqs == [Split]:
                        val = Split(val)
                return val

            value = get_nested_results(result, nested_seqs=nested_sequences)

        return value

    def cast(self, new_type: TypeOrAny) -> "LazyField":
        """ "casts" the lazy field to a new type

        Parameters
        ----------
        new_type : type
            the type to cast the lazy-field to

        Returns
        -------
        cast_field : LazyField
            a copy of the lazy field with the new type
        """
        return LazyField[new_type](
            name=self.name,
            field=self.field,
            attr_type=self.attr_type,
            type=new_type,
        )

    def split(self, splitter: Splitter) -> "LazyField":
        """ "Splits" the lazy field over an array of nodes by replacing the sequence type
        of the lazy field with Split to signify that it will be "split" across

        Parameters
        ----------
        splitter : str or ty.Tuple[str, ...] or ty.List[str]
            the splitter to append to the list of splitters
        """
        from ..utils.typing import TypeParser  # pylint: disable=import-outside-toplevel

        if self.type is ty.Any:
            type_ = Split[ty.Any]
        else:
            try:
                item_type = TypeParser.get_item_type(self.type)
            except TypeError as e:
                add_exc_note(e, f"Attempting to split {self} over multiple nodes")
                raise e
            type_ = Split[item_type]  # type: ignore
        if isinstance(splitter, list):
            splits = set(splitter)
        elif splitter is not None:
            splits = set([splitter])
        else:
            splits = []
        splits |= self.splits
        return LazyField[type_](
            name=self.name,
            field=self.field,
            attr_type=self.attr_type,
            type=type_,
            splits=splits,
        )

    # def combine(self, combiner=None) -> "LazyField":
    #     """ "Combines" the lazy field over an array of nodes by wrapping the type of the
    #     lazy field in a list to signify that it will be actually a list of
    #     values of that type
    #     """
    #     if combiner is not None:
    #         splits = [s for s in self.splits if s != combiner]
    #         if splits == self.splits:
    #             raise ValueError(
    #                 f"{combiner} wasn't found in list of splits for {self}: {self.splits}"
    #             )
    #     type_ = ty.List[self.type]
    #     return LazyField[type_](
    #         name=self.name,
    #         field=self.field,
    #         attr_type=self.attr_type,
    #         type=type_,
    #         splits=splits,
    #     )


class Split(ty.List[T]):
    """an array of values from, or to be split over in an array of nodes (see TaskBase.split()),
    multiple nodes of the same task. Used in type-checking to differentiate between list
    types and values for multiple nodes
    """

    def __repr__(self):
        return f"{type(self).__name__}(" + ", ".join(repr(i) for i in self) + ")"


def donothing(*args, **kwargs):
    return None


@attr.s(auto_attribs=True, kw_only=True)
class TaskHook:
    """Callable task hooks."""

    pre_run_task: ty.Callable = donothing
    post_run_task: ty.Callable = donothing
    pre_run: ty.Callable = donothing
    post_run: ty.Callable = donothing

    def __setattr__(self, attr, val):
        if attr not in ["pre_run_task", "post_run_task", "pre_run", "post_run"]:
            raise AttributeError("Cannot set unknown hook")
        super().__setattr__(attr, val)

    def reset(self):
        for val in ["pre_run_task", "post_run_task", "pre_run", "post_run"]:
            setattr(self, val, donothing)


def path_to_string(value):
    """Convert paths to strings."""
    if isinstance(value, Path):
        value = str(value)
    elif isinstance(value, list) and len(value) and isinstance(value[0], Path):
        value = [str(val) for val in value]
    return value


from . import core  # noqa
