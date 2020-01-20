import ray
from collections import defaultdict
# import torch


@ray.remote
class StatefulPatientActor:
    """
    This actor is responsible for storing data for a single patient.
    It is assigned to an individual patient and is periodically responsible
    for sending data for inference to the ensemble pipeline.
    Note this actor has an event loop running.
    """

    def __init__(self, patient_name, pipeline,
                 periodic_interval,
                 supported_vtype="ECG"):
        self.pipeline = pipeline
        self.patient_name = patient_name
        # when to make predicitons
        self.periodic_interval = periodic_interval
        # vtype -> [val1, val2, val3, ..]
        self.patient_data = {}
        # value_type: ECG (supported right now), vitals etc.
        self.supported_vtypes = supported_vtype
        print("ACTOR STARTED: {}, {}".format(
            self.patient_name, self.patient_data))

    def get_periodic_predictions(self, info):
        # print(self.pipeline)
        # return info
        patient_name = info["patient_name"]
        assert patient_name == self.patient_name
        value = info["value"]
        value_type = info["vtype"]
        result = ""
        import torch
        if value_type == self.supported_vtypes:
            result = torch.tensor([value])
        return result

        # append the data point to the patient's stored data structure

        # if value_type in self.patient_data:
        #     self.patient_data[value_type].append(torch.tensor([[value]]))
        # else:
        #     self.patient_data[value_type] = [torch.tensor([[value]])]

        # if len(patient_val_list) == self.periodic_interval:
        #     data = torch.cat(patient_val_list, dim=1)
        #     data = torch.stack([data])
        #     patient_val_list.clear()
        #     result = ray.get(self.pipeline.remote(data=data))
