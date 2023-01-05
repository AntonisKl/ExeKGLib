import os
from typing import Union, List, Dict

import pandas as pd
from rdflib import Literal

from utils.cli_utils import (
    get_input_for_existing_data_entities,
    get_input_for_new_data_entities,
)
from utils.kg_creation_utils import (
    add_literal,
    add_instance_from_parent_with_relation,
    name_instance,
    add_data_entity_instance,
    add_and_attach_data_entity,
    create_pipeline_task,
)
from utils.query_utils import *
from utils.query_utils import (
    get_data_properties_plus_inherited_by_class_iri,
    get_pipeline_and_first_task_iri,
    get_method_by_task_iri,
)
from utils.string_utils import property_name_to_field_name
from .data_entity import DataEntity
from .entity import Entity
from .kg_schema import KGSchema
from .task import Task
from .tasks import visual_tasks, statistic_tasks, ml_tasks

KG_SCHEMAS = {
    "Data Science": {
        "path": "https://raw.githubusercontent.com/nsai-uio/ExeKGOntology/main/ds_exeKGOntology.ttl",
        "namespace": "https://raw.githubusercontent.com/nsai-uio/ExeKGOntology/main/ds_exeKGOntology.ttl#",
        "namespace_prefix": "ds",
    },
    "Visualization": {
        "path": "https://raw.githubusercontent.com/nsai-uio/ExeKGOntology/main/visu_exeKGOntology.ttl",
        "namespace": "https://raw.githubusercontent.com/nsai-uio/ExeKGOntology/main/visu_exeKGOntology.ttl#",
        "namespace_prefix": "visu",
    },
    "Statistics": {
        "path": "https://raw.githubusercontent.com/nsai-uio/ExeKGOntology/main/stats_exeKGOntology.ttl",
        "namespace": "https://raw.githubusercontent.com/nsai-uio/ExeKGOntology/main/stats_exeKGOntology.ttl#",
        "namespace_prefix": "stats",
    },
    "Machine Learning": {
        "path": "https://raw.githubusercontent.com/nsai-uio/ExeKGOntology/main/ml_exeKGOntology.ttl",
        "namespace": "https://raw.githubusercontent.com/nsai-uio/ExeKGOntology/main/ml_exeKGOntology.ttl#",
        "namespace_prefix": "ml",
    },
}


class ExeKG:
    def __init__(self, kg_schema_name: str = None, input_exe_kg_path: str = None):
        """

        Args:
            kg_schema_name: name of chosen bottom-level KG schema to use in case of KG construction (must be equal to one of KG_SCHEMAS keys)
                            acts as switch for KG construction mode (if filled, mode is on)
            input_exe_kg_path: path of KG to be executed
                               acts as switch for KG execution mode (if filled, mode is on)
        """
        self.top_level_schema = KGSchema.from_schema_info(KG_SCHEMAS["Data Science"])  # top-level KG schema

        # top-level KG schema entities
        self.atomic_task = Entity(self.top_level_schema.namespace.AtomicTask)
        self.atomic_method = Entity(self.top_level_schema.namespace.AtomicMethod)
        self.data_entity = Entity(self.top_level_schema.namespace.DataEntity)
        self.pipeline = Entity(self.top_level_schema.namespace.Pipeline)
        self.data = Entity(self.top_level_schema.namespace.Data)
        self.data_semantics = Entity(self.top_level_schema.namespace.DataSemantics)
        self.data_structure = Entity(self.top_level_schema.namespace.DataStructure)

        # self.input_kg: KG eventually filled with 3 KG schemas and the input executable KG in case of KG execution
        self.input_kg = Graph(bind_namespaces="rdflib")
        if input_exe_kg_path:  # KG execution mode
            self.input_kg.parse(input_exe_kg_path, format="n3")  # parse input executable KG
            all_ns = [n for n in self.input_kg.namespace_manager.namespaces()]
            bottom_level_schema_info_set = False  # flag indicating that a bottom-level schema was found
            for schema_name, schema_info in KG_SCHEMAS.items():  # search for used bottom-level schema
                if schema_name == "Data Science" or schema_name == "Visualization":  # skip top-level KG schema and Visualization schema that is always used
                    continue

                if (schema_info["namespace_prefix"], URIRef(schema_info["namespace"])) in all_ns:
                    # bottom-level schema found
                    self.bottom_level_schema = KGSchema.from_schema_info(schema_info)
                    bottom_level_schema_info_set = True
                    break

            visu_schema_info = KG_SCHEMAS["Visualization"]
            if (
                    not bottom_level_schema_info_set
                    and (visu_schema_info["namespace_prefix"], URIRef(visu_schema_info["namespace"])) in all_ns
            ):  # Visualization schema is considered the bottom-level schema ONLY IF no other bottom-level schema was found
                self.bottom_level_schema = KGSchema.from_schema_info(visu_schema_info)
                bottom_level_schema_info_set = True

            if not bottom_level_schema_info_set:  # no bottom-level schema found, input executable KG is invalid
                print("Input executable KG did not have any bottom level KG schemas")
                exit(1)
        else:  # KG construction mode
            # bottom-level schema used as compatibility guide for constructing executable KG
            self.bottom_level_schema = KGSchema.from_schema_info(KG_SCHEMAS[kg_schema_name])

        self.visu_schema = KGSchema.from_schema_info(
            KG_SCHEMAS["Visualization"])  # Visualization KG schema, always used

        self.input_kg += self.top_level_schema.kg + self.bottom_level_schema.kg + self.visu_schema.kg  # combine all KG schemas in input KG

        self.output_kg = Graph(bind_namespaces="rdflib")  # KG to be filled while constructing executable KG

        self.bind_used_namespaces([self.input_kg, self.output_kg])

        # below variables are filled in self.parse_kgs()
        self.task_type_dict = {}  # dict for uniquely naming each new pipeline task
        self.method_type_dict = {}  # dict for uniquely naming each new pipeline method
        self.atomic_task_list = []  # list for storing the available sub-classes of ds:AtomicTask
        self.atomic_method_list = []  # list for storing the available sub-classes of ds:AtomicMethod
        self.data_type_list = []  # list for storing the available sub-classes of ds:DataEntity
        self.data_semantics_list = []  # list for storing the available sub-classes of ds:DataSemantics
        self.data_structure_list = []  # list for storing the available sub-classes of ds:DataStructure

        self.last_created_task = None  # last created pipeline task, for connecting consecutive pipeline tasks during KG construction

        self.parse_kgs()

    def bind_used_namespaces(self, kgs: List[Graph]):
        """
        Binds top-level, bottom-level and Visualization KG schemas' namespaces with their prefixes
        Adds these bindings to the Graphs of kgs list
        Args:
            kgs: list of Graph objects to which the namespace bindings are added
        """
        for kg in kgs:
            kg.bind(
                self.top_level_schema.namespace_prefix, self.top_level_schema.namespace
            )
            kg.bind(
                self.bottom_level_schema.namespace_prefix,
                self.bottom_level_schema.namespace,
            )
            kg.bind(
                self.visu_schema.namespace_prefix,
                self.visu_schema.namespace,
            )

    def parse_kgs(self) -> None:
        """
        Fills lists with subclasses of top-level KG schema classes and initializes dicts used for unique naming
        """
        atomic_task_subclasses = get_subclasses_of(self.atomic_task.iri, self.input_kg)
        for t in list(atomic_task_subclasses):
            task = Entity(t[0], self.atomic_task)
            self.atomic_task_list.append(task)
            self.task_type_dict[task.name] = 1

        atomic_method_subclasses = get_subclasses_of(
            self.atomic_method.iri, self.input_kg
        )
        for m in list(atomic_method_subclasses):
            method = Entity(m[0], self.atomic_method)
            self.atomic_method_list.append(method)
            self.method_type_dict[method.name] = 1

        data_type_subclasses = get_subclasses_of(self.data_entity.iri, self.input_kg)
        for d in list(data_type_subclasses):
            data_type = Entity(d[0], self.data_entity)
            self.data_type_list.append(data_type)

        data_semantics_subclasses = get_subclasses_of(
            self.data_semantics.iri, self.top_level_schema.kg
        )
        for d in list(data_semantics_subclasses):
            if d[0] == self.data_entity.iri:
                continue
            data_semantics = Entity(d[0], self.data_semantics)
            self.data_semantics_list.append(data_semantics)

        data_structure_subclasses = get_subclasses_of(
            self.data_structure.iri, self.top_level_schema.kg
        )
        for d in list(data_structure_subclasses):
            if d[0] == self.data_entity.iri:
                continue
            data_structure = Entity(d[0], self.data_structure)
            self.data_structure_list.append(data_structure)

    def create_pipeline_task(self, pipeline_name: str, input_data_path: str) -> Task:
        """
        Instantiates and adds a new pipeline task entity to self.output_kg
        Args:
            pipeline_name: name for the pipeline
            input_data_path: path for the input data to be used by the pipeline's tasks

        Returns:
            Task: created pipeline
        """
        pipeline = create_pipeline_task(
            self.top_level_schema.namespace,
            self.bottom_level_schema.namespace,
            self.pipeline,
            self.output_kg,
            pipeline_name,
            input_data_path,
        )
        self.last_created_task = pipeline
        return pipeline

    def create_data_entity(
            self,
            name: str,
            source_value: str,
            data_semantics_name: str,
            data_structure_name: str,
    ) -> DataEntity:
        """
        Creates a DataEntity object
        Args:
            name: name of the data entity
            source_value: name of the data source corresponding to a column of the data
            data_semantics_name: name of the data semantics entity
            data_structure_name: name of the data structure entity

        Returns:
            DataEntity: object initialized with the given parameter values
        """
        return DataEntity(
            self.bottom_level_schema.namespace + name,
            self.data_entity,
            source_value,
            self.top_level_schema.namespace + data_semantics_name,
            self.top_level_schema.namespace + data_structure_name,
        )

    def add_task(
            self,
            task_type: str,
            input_data_entity_dict: Dict[str, List[DataEntity]],
            method_type: str,
            data_properties: Dict[str, Union[str, int, float]],
            visualization: bool = False,
    ) -> Task:
        """
        Instantiates and adds a new task entity to self.output_kg
        Components added to the task during creation: input and output entities, and a method with data properties
        Args:
            task_type: type of the task
            input_data_entity_dict: keys -> input entity names corresponding to the given task_type as defined in the chosen bottom-level KG schema
                                    values -> list of corresponding data entities to be added as input to the task
            method_type: type of the task's method
            data_properties: keys -> data property names corresponding to the given method_type as defined in the chosen bottom-level KG schema
                             values -> list of corresponding values to be added as property values to the task
            visualization: if True, the namespace prefix of Visualization KG schema is used during creation of the task
                           else, the namespace prefix of the chosen bottom-level KG schema is used

        Returns:
            Task: object of the created task
        """
        namespace_to_use = (
            self.visu_schema.namespace
            if visualization
            else self.bottom_level_schema.namespace
        )  # use appropriate namespace for the task

        relation_iri = (
            self.top_level_schema.namespace.hasNextTask
            if self.last_created_task.type != "Pipeline"
            else self.top_level_schema.namespace.hasStartTask
        )  # use relation depending on the previous task

        # instantiate task and link it with the previous one
        parent_task = Task(namespace_to_use + task_type, self.atomic_task)
        added_entity = add_instance_from_parent_with_relation(
            self.bottom_level_schema.namespace,
            self.output_kg,
            parent_task,
            relation_iri,
            self.last_created_task,
            name_instance(self.task_type_dict, self.method_type_dict, parent_task),
        )
        next_task = Task.from_entity(added_entity)  # create Task object from Entity object

        # instantiate and add given input data entities to the task
        self.add_inputs_to_task(next_task, input_data_entity_dict)
        # instantiate and add output data entities to the task, as specified in the KG schema
        self.add_outputs_to_task(next_task)

        method_parent = Entity(namespace_to_use + method_type, self.atomic_method)

        # fetch compatible methods and their properties from KG schema
        results = list(
            get_method_properties_and_methods(
                self.input_kg,
                self.top_level_schema.namespace_prefix,
                next_task.parent_entity.iri,
            )
        )

        chosen_property_method = next(
            filter(lambda pair: pair[1].split("#")[1] == method_type, results), None
        )  # match given method_type with query result
        if chosen_property_method is None:
            print(
                f"Property connecting task of type {task_type} with method of type {method_type} not found"
            )
            exit(1)

        # instantiate method and link it with the task using the appropriate chosen_property_method[0] relation
        add_instance_from_parent_with_relation(
            self.bottom_level_schema.namespace,
            self.output_kg,
            method_parent,
            chosen_property_method[0],
            next_task,
            name_instance(self.task_type_dict, self.method_type_dict, method_parent),
        )

        # fetch compatible data properties from KG schema
        property_list = get_data_properties_plus_inherited_by_class_iri(
            self.input_kg, method_parent.iri
        )

        # add data properties to the task with given values
        for pair in property_list:
            property_iri = pair[0]
            property_name = property_iri.split("#")[1]
            range_iri = pair[1]
            input_property = Literal(
                lexical_or_value=data_properties[property_name],
                datatype=range_iri,
            )
            add_literal(self.output_kg, next_task, property_iri, input_property)

        self.last_created_task = next_task  # store created task

        return next_task

    def add_inputs_to_task(
            self, task_entity: Task, input_data_entity_dict: dict[str, List[DataEntity]]
    ) -> None:
        """
        Instantiates and adds given input data entities to the given task of self.output_kg
        Args:
            task_entity: the task to add the input to
            input_data_entity_dict: keys -> input entity names corresponding to the given task as defined in the chosen bottom-level KG schema
                                    values -> list of corresponding data entities to be added as input to the task
        """
        # fetch compatible inputs from KG schema
        results = list(
            get_input_properties_and_inputs(
                self.input_kg,
                self.top_level_schema.namespace_prefix,
                task_entity.parent_entity.iri,
            )
        )

        # task_type_index was incremented when creating the task entity
        # reset the index to match the currently created task's index
        task_type_index = self.task_type_dict[task_entity.type] - 1
        for _, input_entity_iri in results:
            input_entity_name = input_entity_iri.split("#")[1]
            input_data_entity_list = input_data_entity_dict[input_entity_name]

            same_input_index = 1
            for input_data_entity in input_data_entity_list:
                # instantiate data entity corresponding to the found input_entity_name
                data_entity_iri = (
                        input_entity_iri
                        + str(task_type_index)
                        + "_"
                        + str(same_input_index)
                )
                # instantiate given data entity
                add_data_entity_instance(
                    self.output_kg,
                    self.data,
                    self.top_level_schema.kg,
                    self.top_level_schema.namespace,
                    input_data_entity,
                )
                # instantiate and attach data entity with reference to the given data entity
                data_entity = DataEntity(
                    data_entity_iri,
                    DataEntity(input_entity_iri, self.data_entity),
                    has_reference=input_data_entity.iri,
                )
                add_and_attach_data_entity(
                    self.output_kg,
                    self.data,
                    self.top_level_schema.kg,
                    self.top_level_schema.namespace,
                    data_entity,
                    self.top_level_schema.namespace.hasInput,
                    task_entity,
                )
                task_entity.input_dict[input_entity_name] = data_entity
                same_input_index += 1

    def add_outputs_to_task(self, task_entity: Task) -> None:
        """
        Instantiates and adds output data entities to the given task of self.output_kg, based on the task's definition in the KG schema
        Args:
            task_entity: the task to add the output to
        """
        # fetch compatible outputs from KG schema
        results = list(
            get_output_properties_and_outputs(
                self.input_kg,
                self.top_level_schema.namespace_prefix,
                task_entity.parent_entity.iri,
            )
        )

        # task_type_index was incremented when creating the task entity
        # reset the index to match the currently created task's index
        task_type_index = self.task_type_dict[task_entity.type] - 1
        for output_property, output_entity_iri in results:
            # instantiate and add data entity
            data_entity_iri = output_entity_iri + str(task_type_index)
            data_entity = DataEntity(data_entity_iri, self.data_entity)
            add_and_attach_data_entity(
                self.output_kg,
                self.data,
                self.top_level_schema.kg,
                self.top_level_schema.namespace,
                data_entity,
                self.top_level_schema.namespace.hasOutput,
                task_entity,
            )
            task_entity.output_dict[output_entity_iri.split("#")[1]] = data_entity

    def create_next_task_cli(self, prev_task: Task, existing_data_entity_list: List[DataEntity]) -> Union[None, Task]:
        """
        Instantiates and adds task (without method) based on user input to self.output_kg
        Adds task's output data entities to existing_data_entity_list
        Args:
            prev_task: previously added task
            existing_data_entity_list: list of existing data entities that are output entities of previous tasks

        Returns:
            None: in case user wants to end the pipeline creation
            Task: object of the created task
        """
        print("Please choose the next task")
        for i, t in enumerate(self.atomic_task_list):
            print("\t{}. {}".format(str(i), t.name))
        print("\t{}. End pipeline".format(str(-1)))
        next_task_id = int(input())
        if next_task_id == -1:
            return None

        next_task_parent = self.atomic_task_list[next_task_id]
        relation_iri = (
            self.top_level_schema.namespace.hasNextTask
            if prev_task.type != "Pipeline"
            else self.top_level_schema.namespace.hasStartTask
        )  # use relation depending on the previous task

        # instantiate task and link it with the previous one
        task_entity = add_instance_from_parent_with_relation(
            self.bottom_level_schema.namespace,
            self.output_kg,
            next_task_parent,
            relation_iri,
            prev_task,
            name_instance(self.task_type_dict, self.method_type_dict, next_task_parent),
        )

        task_entity = Task(task_entity.iri, task_entity.parent_entity)  # create Task object from Entity object's info

        # get user's choice of existing data entities to use as input
        chosen_data_entity_list = get_input_for_existing_data_entities(
            existing_data_entity_list
        )
        for chosen_data_entity in chosen_data_entity_list:
            # instantiate and attach data entity corresponding to the found input_entity_name
            add_and_attach_data_entity(
                self.output_kg,
                self.data,
                self.top_level_schema.kg,
                self.top_level_schema.namespace,
                chosen_data_entity,
                self.top_level_schema.namespace.hasInput,
                task_entity,
            )
            task_entity.has_input.append(chosen_data_entity)

        # get user's input for using a new data entity as input
        (
            source_list,
            data_semantics_iri_list,
            data_structure_iri_list,
        ) = get_input_for_new_data_entities(
            self.data_semantics_list, self.data_structure_list
        )

        for source, data_semantics_iri, data_structure_iri in zip(
                source_list, data_semantics_iri_list, data_structure_iri_list
        ):
            # instantiate and attach given data entity
            data_entity = DataEntity(
                self.bottom_level_schema.namespace + source,
                self.data_entity,
                source,
                data_semantics_iri,
                data_structure_iri,
            )
            add_and_attach_data_entity(
                self.output_kg,
                self.data,
                self.top_level_schema.kg,
                self.top_level_schema.namespace,
                data_entity,
                self.top_level_schema.namespace.hasInput,
                task_entity,
            )
            task_entity.has_input.append(data_entity)
            existing_data_entity_list.append(data_entity)

        # instantiate and add output data entities to the task, as specified in the KG schema
        self.add_outputs_to_task(task_entity)

        return task_entity

    def create_method(self, task_to_attach_to: Entity) -> None:
        """
        Instantiate and attach method to task of self.output_kg
        Args:
            task_to_attach_to: the task to attach the created method to
        """
        print("Please choose a method for {}:".format(task_to_attach_to.type))

        # fetch compatible methods and their properties from KG schema
        results = list(
            get_method_properties_and_methods(
                self.input_kg,
                self.top_level_schema.namespace_prefix,
                task_to_attach_to.parent_entity.iri,
            )
        )
        for i, pair in enumerate(results):
            tmp_method = pair[1].split("#")[1]
            print("\t{}. {}".format(str(i), tmp_method))

        method_id = int(input())
        selected_property_and_method = results[method_id]
        method_parent = next(
            filter(
                lambda m: m.iri == selected_property_and_method[1],
                self.atomic_method_list,
            ),
            None,
        )
        # instantiate method and link it with the task using the appropriate selected_property_and_method[0] relation
        add_instance_from_parent_with_relation(
            self.bottom_level_schema.namespace,
            self.output_kg,
            method_parent,
            selected_property_and_method[0],
            task_to_attach_to,
            name_instance(self.task_type_dict, self.method_type_dict, method_parent),
        )

        # fetch compatible data properties from KG schema
        property_list = get_data_properties_plus_inherited_by_class_iri(
            self.input_kg, method_parent.iri
        )

        if property_list:
            print(
                "Please enter requested properties for {}:".format(method_parent.name)
            )
            # add data properties to the task with given values
            for pair in property_list:
                property_instance = URIRef(pair[0])
                range = pair[1].split("#")[1]
                range_iri = pair[1]
                input_property = Literal(
                    lexical_or_value=input(
                        "\t{} in range({}): ".format(pair[0].split("#")[1], range)
                    ),
                    datatype=range_iri,
                )
                add_literal(
                    self.output_kg, task_to_attach_to, property_instance, input_property
                )

    def start_pipeline_creation(self, pipeline_name: str, input_data_path: str) -> None:
        """
        Handles the pipeline creation through CLI
        Args:
            pipeline_name: name for the pipeline
            input_data_path: path for the input data to be used by the pipeline's tasks
        """
        pipeline = create_pipeline_task(
            self.top_level_schema.namespace,
            self.bottom_level_schema.namespace,
            self.pipeline,
            self.output_kg,
            pipeline_name,
            input_data_path,
        )

        prev_task = pipeline
        data_entities_list = pipeline.has_input
        while True:
            next_task = self.create_next_task_cli(prev_task, data_entities_list)
            if next_task is None:
                break

            self.create_method(next_task)

            prev_task = next_task

    def save_created_kg(self, file_path: str) -> None:
        """
        Saves self.output_kg to a file
        Args:
            file_path: path of the output file
        """
        dir_path = os.path.dirname(file_path)
        os.makedirs(dir_path, exist_ok=True)

        self.output_kg.serialize(destination=file_path)
        print(f"Executable KG saved in {file_path}")

    def property_value_to_field_value(
            self, property_value: str
    ) -> Union[str, DataEntity]:
        """
        Converts property value to Python class field value
        If property_value is not a data entity's IRI, it is returned as is
        Else, its property values are converted recursively and stored in a DataEntity object
        Args:
            property_value: value of the property as found in KG

        Returns:
            str: property_value parameter as is
            DataEntity: object containing parsed data entity properties
        """
        if "#" in property_value:
            data_entity = self.parse_data_entity_by_iri(property_value)
            if data_entity is None:
                return property_value
            return data_entity

        return property_value

    def parse_data_entity_by_iri(
            self, in_out_data_entity_iri: str
    ) -> Optional[DataEntity]:
        """
        Parses an input or output data entity of self.input_kg and stores the parsed info in a Python object
        Args:
            in_out_data_entity_iri: IRI of the KG entity to parse

        Returns:
            None: if given IRI does not belong to an instance of a sub-class of self.top_level_schema.namespace.DataEntity
            DataEntity: object with data entity's parsed properties
        """
        # fetch type of entity with given IRI
        query_result = get_first_query_result_if_exists(
            query_entity_parent_iri,
            self.input_kg,
            in_out_data_entity_iri,
            self.top_level_schema.namespace.DataEntity,
        )
        if query_result is None:
            return None

        data_entity_parent_iri = str(query_result[0])

        # fetch IRI of data entity that is referenced by the given entity
        query_result = get_first_query_result_if_exists(
            query_data_entity_reference_iri,
            self.input_kg,
            self.top_level_schema.namespace_prefix,
            in_out_data_entity_iri,
        )

        if query_result is None:  # no referenced data entity found
            data_entity_ref_iri = in_out_data_entity_iri
        else:
            data_entity_ref_iri = str(query_result[0])

        # create DataEntity object to store all the parsed properties
        data_entity = DataEntity(in_out_data_entity_iri, Entity(data_entity_parent_iri))
        data_entity.has_reference = data_entity_ref_iri.split("#")[1]

        for s, p, o in self.input_kg.triples((URIRef(data_entity_ref_iri), None, None)):
            # parse property name and value
            field_name = property_name_to_field_name(str(p))
            if not hasattr(data_entity, field_name) or field_name == "type":
                continue
            field_value = self.property_value_to_field_value(str(o))
            setattr(data_entity, field_name, field_value)  # set field value dynamically

        return data_entity

    def parse_task_by_iri(
            self, task_iri: str, canvas_method: visual_tasks.CanvasTaskCanvasMethod = None
    ) -> Task:
        """
        Parses a task of self.input_kg and stores the info in an object of a sub-class of Task
        The sub-class name and the object's fields are mapped dynamically based on the found KG components
        Args:
            task_iri: IRI of the task to be parsed
            canvas_method: optional object to pass as argument for task object initialization

        Returns:
            Task: object of a sub-class of Task, containing all the parsed info
        """
        # fetch type of entity with given IRI
        query_result = get_first_query_result_if_exists(
            query_entity_parent_iri,
            self.input_kg,
            task_iri,
            self.top_level_schema.namespace.AtomicTask,
        )

        if query_result is None:  # given IRI does not belong to an instance of a sub-class of self.top_level_schema.namespace.AtomicTask
            print(f"Cannot retrieve parent of task with iri {task_iri}. Exiting...")
            exit(1)

        task_parent_iri = str(query_result[0])

        task = Task(task_iri, Task(task_parent_iri))
        method = get_method_by_task_iri(
            self.input_kg,
            self.top_level_schema.namespace_prefix,
            self.top_level_schema.namespace,
            task_iri,
        )
        if method is None:
            print(f"Cannot retrieve method for task with iri: {task_iri}")

        # perform automatic mapping of KG task class to Python sub-class
        class_name = task.type + method.type
        Class = getattr(visual_tasks, class_name, None)
        if Class is None:
            Class = getattr(statistic_tasks, class_name, None)
        if Class is None:
            Class = getattr(ml_tasks, class_name, None)

        # create Task sub-class object
        if canvas_method:
            task = Class(task_iri, Task(task_parent_iri), canvas_method)
        else:
            task = Class(task_iri, Task(task_parent_iri))

        for s, p, o in self.input_kg.triples((URIRef(task_iri), None, None)):
            # parse property name and value
            field_name = property_name_to_field_name(str(p))
            if not hasattr(task, field_name) or field_name == "type":
                continue
            field_value = self.property_value_to_field_value(str(o))

            # set field value dynamically
            if field_name == "has_input" or field_name == "has_output":
                getattr(task, field_name).append(field_value)
            else:
                setattr(task, field_name, field_value)

        return task

    def execute_pipeline(self):
        """
        Retrieves and executes pipeline by parsing self.input_kg
        """
        pipeline_iri, input_data_path, next_task_iri = get_pipeline_and_first_task_iri(
            self.input_kg, self.top_level_schema.namespace_prefix
        )
        input_data = pd.read_csv(input_data_path, delimiter=",", encoding="ISO-8859-1")
        canvas_method = None  # stores Task object that corresponds to a task of type CanvasTask
        task_output_dict = {}  # gradually filled with outputs of executed tasks
        while next_task_iri is not None:
            next_task = self.parse_task_by_iri(next_task_iri, canvas_method)
            output = next_task.run_method(task_output_dict, input_data)
            if output:
                task_output_dict.update(output)

            if next_task.type == "CanvasTask":
                canvas_method = next_task

            next_task_iri = next_task.has_next_task

    @staticmethod
    def input_kg_schema_name() -> str:
        """
        Prompts the user to choose a schema by presenting the available schemas' names
        Returns:
            str: chosen schema name
        """
        kg_schema_names = list(KG_SCHEMAS.keys())
        print(
            "Choose a KG schema to use. Components of the Visualization schema can be used regardless of the chosen schema.")
        for i, kg_schema_name in enumerate(kg_schema_names):
            print(f"{i}: {kg_schema_name}")
        selected_schema_i = int(input())
        selected_schema_name = kg_schema_names[selected_schema_i]

        return selected_schema_name
