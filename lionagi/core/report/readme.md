# Report

### Basic Usage Pattern

The states associated with a `Task` is saved in a `Report`,

LNDL uses declarative approach, you can declare your own tasks and desired output by inheriting creating a class inheriting from `Report`,

Here is a simple example. 

```python
from lionagi.experimental.report.report import Report

class Report1(Report):
    assignment: str = "a, b -> f"
    
    form_assignments: list = [
        "a, b -> c", 
        "c -> d", 
        "b, d -> e",
        "d, e -> f"
    ]
```

Above means, 

The overall objective is : 
> given a, b produce f

The work steps are specified as
> given a, b , produce c

> given c, produce d

> given b, d, produce e

> given d, e, produce f

The overall objective now is broken down into different work steps. Each unique `form_assignment` , represents a specific type of `Work`, similar to how `Report` represents `Task`, when the `Work` is performed, all of its associated states are recorded on a `Form`. 

## What's Happening?

### Input Validation

As inputs arrive for a `Worker` to handle, it first gets validated by `Validator`. The inputs can be existing work in progress `Report` that get passed down to current `Worker`, other types of input includes named data and custom objects, such as `Node`, which requires special handling. 

You can customize how each field gets validated by adding `Rule` to the `Validator`, for example, you can require field `a` needs to be an integer between 0 to 3, inclusive, if the value is not valid, you can specify the methods to handle, such as discard, keep in record, fix input...etc 

if the validation get passed, the worker creates a report

### Form Creation

Upon initialization, the report creates forms:

```python
form1 = Form1(assignment="a, b -> c")
form2 = Form2(assignment="c -> d")
form3 = Form3(assignment="b, d -> e")
form4 = Form4(assignment="d, e -> f")
```

These forms could be instances of either a single form class or multiple distinct classes, provided they interface correctly with the same fields. By default, each `Worker` will carry `Form` as template to work on. 

As `Form` gets instantiated, it only contains the relevant fields, for example `form1` will only have 3 `work_field`, a, b and c.  form2 only have c, d... and so on. The forms are created with value None. 

The validator will `fill` the validated data into form and report, and after each time it does so, it checks the `next_forms` of the report to process. 

### Form Scheduling

Initially, the fields `a` and `b` are available:
- Only form1 is `workable` meaning it has everything it needs to be processed
- The form gets passed to `WorkFunction` to process
- As each form is completed, it is sent back to the validator, which validate the data, deciding whether filling them, or do something else. If it does fill in the results, then there are new `new_forms` to work on basing on newly updated information. 

