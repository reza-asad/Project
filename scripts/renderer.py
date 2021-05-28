import numpy as np
import pyrender
from pyrender import RenderFlags
import gc


class Render:
    def __init__(self, kwargs, camera_pose=None):
        self.kwargs = kwargs
        self.camera_pose = camera_pose

    def pyrender_render(self, scene, resolution, camera_pose, room_dimension):
        length, width, height = room_dimension
        # convert trimesh scene to pyrender scene
        scene = pyrender.Scene.from_trimesh_scene(scene)

        # create and adjust one point light source and one directional one.
        light_directional = pyrender.DirectionalLight(color=[255.0, 255.0, 255.0],
                                                      intensity=self.kwargs['light_directional_intensity'])
        light_directional_pose = camera_pose.copy()
        light_directional_pose[2, 3] = height - self.kwargs['wall_thickness']

        light_point_spot_center = pyrender.SpotLight(color=[255.0, 255.0, 255.0],
                                                     intensity=self.kwargs['light_point_intensity_center'],
                                                     innerConeAngle=np.pi/2, outerConeAngle=np.pi/2)
        light_point_spot_pose_center = camera_pose.copy()
        light_point_spot_pose_center[2, 3] = height - self.kwargs['wall_thickness']

        max_dim = np.maximum(length, width)
        x = np.sqrt(max_dim**2 + max_dim**2)
        camera_pose[2, 3] = np.maximum(x / (2 * np.tan(self.kwargs['fov'] / 2)) + height,
                                       camera_pose[2, 3] + height)

        # ensure the near plane of camera passes the ceiling
        z_near = camera_pose[2, 3] - height - self.kwargs['wall_thickness']
        i = 1
        while z_near < 0:
            z_near = camera_pose[2, 3] - height - self.kwargs['wall_thickness'] / 2**i
            i += 1

        # create and adjust camera
        camera = pyrender.PerspectiveCamera(yfov=self.kwargs['fov'], aspectRatio=1)
        camera.znear = z_near

        # add light and camera to the scene
        scene.add(light_directional, pose=light_directional_pose)
        scene.add(light_point_spot_center, pose=light_point_spot_pose_center)
        scene.add(camera, pose=camera_pose)
        self.camera_pose = camera_pose

        # render the image
        flags = RenderFlags.ALL_SOLID
        r = pyrender.OffscreenRenderer(viewport_width=resolution[0], viewport_height=resolution[1])
        img, depth = r.render(scene, flags)
        r.delete()
        del scene
        gc.collect()
        return img
