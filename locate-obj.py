import cv2
from inference_sdk import InferenceHTTPClient
import numpy as np
import re
from collections import deque
import json
import glfw
from OpenGL.GL import *
from OpenGL.GLU import *
import pymap3d.enu as enu
import tkinter as tk
from tkinter import simpledialog
import rasterio
import utm

droneToMundoR = np.array([[0,1,0],[1,0,0],[0,0,-1]])
mundoToDroneR = np.transpose(droneToMundoR)
cameraToDroneR = np.array([[0,0,1],[1,0,0],[0,1,0]])
droneToCameraR = np.transpose(cameraToDroneR)
cameraToMundoR = np.array([[1,0,0],[0,0,1],[0,-1,0]])
mundoToCameraR = np.transpose(cameraToMundoR)
cameraToOpenglR = np.array([[1,0,0],[0,-1,0],[0,0,-1]])

lat0 = -22.905812 
lon0 = -43.221329
h0 = 12.456
utm0_x, utm0_y, utm_zn, utm_zl = utm.from_latlon(lat0, lon0)

dem_interception_epsilon = 0.01
dem_interception_count = 50

near = 0.1
far = 1000.0
cone_height = 5.0
cone_radius = 1.5

minimal_distance_param = 0.01

roi_minimum_confidence = 0.65

glMode = True

# R x = b
def get_rotation_from_vectors(x, b):
    x_norm = norm_vec(x.flatten())
    b_norm = norm_vec(b.flatten())
    v = np.cross(x_norm, b_norm)
    v = norm_vec(v)
    theta = np.arccos(np.clip(np.dot(x_norm, b_norm), -1.0, 1.0))
    theta_op = 2 * np.pi - theta
    if theta <= theta_op:
        rot_vec = theta * v
        R_theta, _ = cv2.Rodrigues(rot_vec)
        return theta, R_theta
    else:
        rot_vec = theta_op * v
        R_theta, _ = cv2.Rodrigues(rot_vec)
        return theta_op, R_theta

def get_roi_data(i):
    root = tk.Tk()
    root.withdraw()
    lat_roi = simpledialog.askfloat(f"Entrada de dados {i}", f"Insira LATITUDE do ROI {i}: ")
    long_roi = simpledialog.askfloat(f"Entrada de dados {i}", f"Insira LONGITUDE do ROI {i}: ")
    h_abs_roi = simpledialog.askfloat(f"Entrada de dados {i}", f"Insira ALTITUDE do ROI {i} em relação ao nível do mar: ")
    return lat_roi, long_roi, h_abs_roi

def inv_K(K):
    fx = K[0][0]
    fy = K[1][1]
    cx = K[0][2]
    cy = K[1][2]
    K_inv = np.array([[1/fx, 0, -cx/fx],
             [0, 1/fy, -cy/fy],
             [0, 0, 1]])
    return K_inv

def norm_vec(v):
    v_copy = v.copy()
    norm_v = v_copy / np.linalg.norm(v_copy)
    return norm_v

def parse_srt(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        srt_content = file.read()

    # Dividir o conteúdo em blocos por frame
    frames = srt_content.strip().split('\n\n')
    frame_data = []

    for frame in frames:
        lines = frame.split('\n')
        
        # Extraindo o índice do frame
        frame_index = int(lines[0])
        
        # Extraindo o intervalo de tempo
        time_range = lines[1].strip()
        start_time, end_time = time_range.split(" --> ")

        # Extraindo o DiffTime
        match_difftime = re.search(r'DiffTime: (\d+)ms', lines[2])
        diff_time_ms = int(match_difftime.group(1))

        # Extraindo data e hora
        data_time = lines[3]

        # Extraindo dados
        matches = re.findall(r'\[(.*?)\]', lines[4])
        data = {}
        for match in matches:
            pairs = match.split()
            for i in range(0, len(pairs) - 1):
                if ':' in pairs[i]:
                    key = pairs[i].replace(":", "")
                    value = pairs[i+1]
                    data[key] = value
        
        frame_data.append({
                'frame_index': frame_index,
                'start_time': start_time,
                'end_time': end_time,
                'diff_time_ms': diff_time_ms,
                'data_time': data_time,
                **data  # Mesclar informações extraídas dos colchetes
            })

    return frame_data

def yaw_pitch_roll_to_rotation_matrix(yaw, pitch, roll):
    # Converter ângulos de graus para radianos
    yaw = np.radians(yaw)
    pitch = np.radians(pitch)
    roll = np.radians(roll)

    # Matrizes de rotação básicas
    Rz = np.array([
        [np.cos(yaw), -np.sin(yaw), 0],
        [np.sin(yaw),  np.cos(yaw), 0],
        [0,            0,           1]
    ])

    Ry = np.array([
        [np.cos(pitch), 0, np.sin(pitch)],
        [0,             1, 0],
        [-np.sin(pitch), 0, np.cos(pitch)]
    ])

    Rx = np.array([
        [1, 0,           0],
        [0, np.cos(roll), -np.sin(roll)],
        [0, np.sin(roll),  np.cos(roll)]
    ])

    # Matriz de rotação composta: R = Rz * Ry * Rx
    R = Rz @ Ry @ Rx
    return R

def find_DEM_intersection(utm_east, utm_north, utm_up, vec_flat_norm):
    count = 0
    while True:
        alt = get_DEM_alt(utm_east, utm_north)
        if alt is None:
            return None
        
        gap = utm_up - alt
        if np.abs(gap) <= dem_interception_epsilon:
            return np.array([[utm_east], [utm_north], [utm_up]])
        
        vec = gap * vec_flat_norm
        utm_east -= vec[0]
        utm_north -= vec[1]
        utm_up -= vec[2]
        
        count += 1
        if count > dem_interception_count:
            return None

def find_ground_intersection(lat, lon, alt, vec):

    # Descompactar vetor
    x, y, z = vec

    # Evitar divisão por zero no vetor
    if z == 0:
        raise ValueError("O vetor é paralelo ao solo e nunca tocará o chão.")

    # Calcular t (tempo escalar para atingir o solo)
    t = -alt / z

    # Coordenadas deslocadas no plano cartesiano
    x_t = t * x
    y_t = t * y

    # Conversão de deslocamento para latitude e longitude
    new_lat = lat + (y_t / 111320)
    new_lon = lon + (x_t / (111320 * np.cos(np.radians(lat))))

    return new_lat, new_lon

def find_ground_intersection_UTM(north, east, alt_rel, alt_abs, vec):

    # Descompactar vetor
    x = vec[0,0]
    y = vec[1,0]
    z = vec[2,0]

    # Evitar divisão por zero no vetor
    if z == 0:
        raise ValueError("O vetor é paralelo ao solo e nunca tocará o chão.")

    # Calcular t (tempo escalar para atingir o solo)
    t = -alt_rel / z

    # Coordenadas deslocadas no plano cartesiano
    x_t = t * x
    y_t = t * y

    # Conversão de deslocamento para UTM
    new_north = north + y_t
    new_east = east + x_t

    return np.array([[new_east], [new_north], [alt_abs - alt_rel]])

def find_ground_intersection_ENU(north, east, alt, vec):

    # Descompactar vetor
    x = vec[0]
    y = vec[1]
    z = vec[2]

    # Evitar divisão por zero no vetor
    if z == 0:
        raise ValueError("O vetor é paralelo ao solo e nunca tocará o chão.")

    # Calcular t (tempo escalar para atingir o solo)
    t = -alt / z

    # Coordenadas deslocadas no plano cartesiano
    x_t = t * x
    y_t = t * y

    # Conversão de deslocamento
    new_north = north + y_t
    new_east = east + x_t

    return np.array([[new_east], [new_north], [0]])

def find_ground_intersection_ECEF(lat, lon, alt, vec, earth_radius=6371000):
    """
    Encontra a latitude e longitude onde o vetor atinge o solo, considerando a curvatura da Terra.
    
    :param lat: Latitude inicial em graus
    :param lon: Longitude inicial em graus
    :param alt: Altitude inicial em metros
    :param vec: Vetor (x, y, z) representando a direção
    :param earth_radius: Raio da Terra em metros
    :return: Nova latitude e longitude em graus
    """
    # Converter latitude, longitude e altitude para coordenadas ECEF (Earth-Centered, Earth-Fixed)
    lat_rad = np.radians(lat)
    lon_rad = np.radians(lon)
    x0 = (earth_radius + alt) * np.cos(lat_rad) * np.cos(lon_rad)
    y0 = (earth_radius + alt) * np.cos(lat_rad) * np.sin(lon_rad)
    z0 = (earth_radius + alt) * np.sin(lat_rad)
    
    # Direção do vetor
    dx, dy, dz = vec
    
    # Resolver interseção do vetor com a superfície esférica da Terra
    # |P + t * D|^2 = R^2
    # P = (x0, y0, z0), D = (dx, dy, dz), R = earth_radius
    # Substituindo: (x0 + t*dx)^2 + (y0 + t*dy)^2 + (z0 + t*dz)^2 = R^2
    a = dx**2 + dy**2 + dz**2
    b = 2 * (x0 * dx + y0 * dy + z0 * dz)
    c = x0**2 + y0**2 + z0**2 - earth_radius**2

    # Resolver a equação quadrática
    discriminant = b**2 - 4 * a * c
    if discriminant < 0:
        raise ValueError("O vetor não atinge a superfície da Terra.")

    # Escolher a menor solução positiva para t (interseção com o solo)
    t = (-b - np.sqrt(discriminant)) / (2 * a)
    if t < 0:
        raise ValueError("O vetor não aponta para a superfície da Terra.")

    # Coordenadas do ponto de interseção em ECEF
    xi = x0 + t * dx
    yi = y0 + t * dy
    zi = z0 + t * dz

    # Converter de ECEF de volta para latitude e longitude
    new_lat = np.degrees(np.arcsin(zi / earth_radius))
    new_lon = np.degrees(np.arctan2(yi, xi))

    return new_lat, new_lon

def reta3D(K_inv, R_t, t, pixel):
    pixel_RP2 = np.array([[pixel[0]], [pixel[1]], [1]])
    p0 = - R_t @ t
    pv = R_t @ K_inv @ pixel_RP2
    return (p0, pv)

def desenhar_centro(image, center_x, center_y, cor, roi_flag=False):
    if (not glMode) or roi_flag:
        line_length = 10
        
        # Desenhar a linha horizontal do '+'
        cv2.line(image, (int(center_x - line_length // 2), center_y), (int(center_x + line_length // 2), center_y),  cor, 2)  # Verde

        # Desenhar a linha vertical do '+'
        cv2.line(image, (center_x, int(center_y - line_length // 2)), (center_x, int(center_y + line_length // 2)),  cor, 2)

def print_on_pixel(image, label, x, y, cor):
    font_scale = 1  # Tamanho da fonte
    font_thickness = 2  # Espessura da fonte
    font = cv2.FONT_HERSHEY_SIMPLEX  # Fonte
    (text_width, text_height), baseline = cv2.getTextSize(label, font, font_scale, font_thickness)
    image_height, image_width, image_channels = image.shape
    text_x = x  # Alinhar à esquerda do retângulo
    text_y = y - baseline - 5  # Acima do retângulo (-5 para espaçamento)

    if text_y < 0:
        text_y = text_height + 5
    if text_x + text_width > image_width:  # Ultrapassa a borda direita
        text_x = image_width - text_width - 5  # Ajustar para a borda direita
    if text_x < 0:  # Ultrapassa a borda esquerda
        text_x = 5  # Ajustar para a borda esquerda


    cv2.putText(image, label, (text_x, text_y), font, font_scale, cor, font_thickness)

def mouse_click(event, x, y, flags, param):
    clicks, clicks_ENU = param
    if event == cv2.EVENT_LBUTTONDOWN:  # Clique com o botão esquerdo
        original_x = int(x * scale_x)
        original_y = int(y * scale_y)
        clicks.append((original_x, original_y))
    elif event == cv2.EVENT_RBUTTONDOWN:  # Clique com o botão direito
        if (len(clicks_ENU) > 0):
            clicks_ENU.popleft()

def build_projection_matrix(K, width, height, near=near, far=far):
    fx, fy = K[0, 0], K[1, 1]
    cx, cy = K[0, 2], K[1, 2]

    proj = np.zeros((4, 4))
    proj[0, 0] = 2 * fx / width
    proj[1, 1] = 2 * fy / height
    proj[0, 2] = 1 - (2 * cx / width)
    proj[1, 2] = 1 - (2 * cy / height)
    proj[2, 2] = -(far + near) / (far - near)
    proj[2, 3] = -2 * far * near / (far - near)
    proj[3, 2] = -1
    return proj

def build_view_matrix(R, t):
    """ Converte R e t para uma matriz de visualização do OpenGL. """
    R_T = np.transpose(R)
    Rt = np.concatenate((R_T, -R_T @ t), axis=1)
    view = np.eye(4)  # Matriz identidade 4x4
    view[:3, :4] = Rt  # Insere [R | t] na matriz 4x4
    return view

def draw_cone_sphere(x, y, z, pitch, color):

    color_array = [0.0, 0.0, 0.0, 1.0]
    if color == "red":
        color_array = [1.0, 0.0, 0.0, 1.0]
    elif color == "blue":
        color_array = [0.0, 0.0, 1.0, 1.0]
    elif color == "green":
        color_array = [0.0, 1.0, 0.0, 1.0]
    elif color == "black":
        color_array = [0.1, 0.1, 0.1, 1.0]

    # Esfera vermelha
    glMaterialfv(GL_FRONT, GL_AMBIENT, color_array)
    glMaterialfv(GL_FRONT, GL_DIFFUSE, color_array)
    glMaterialfv(GL_FRONT, GL_SPECULAR, [1.0, 1.0, 1.0, 1.0])
    glMaterialf(GL_FRONT, GL_SHININESS, 50.0)

    sphere_quadric = gluNewQuadric()
    glPushMatrix()
    glTranslatef(x, y, z)  # **Posicionar no local correto**
    gluSphere(sphere_quadric, cone_radius, 20, 20)
    glPopMatrix()

    # Desabilitar o plano de corte
    glDisable(GL_CLIP_PLANE0)

    # Cone
    cone_quadric = gluNewQuadric()
    glPushMatrix()
    glTranslatef(x, y, z)  # **Mesmo posicionamento para o cone**
    glRotatef(90 - pitch, 1, 0, 0)
    gluCylinder(cone_quadric, cone_radius, 0, cone_height, 20, 20)
    glPopMatrix()

def render(draw_func):

    if glMode:
        glLoadIdentity()
        draw_func()

def draw_opengl(pixels_opengl, imagem_fundo):
    # Capturar a tela do OpenGL
    imagem_renderizada = np.frombuffer(pixels_opengl, dtype=np.uint8).reshape(1080, 1920, 3)
    imagem_renderizada = cv2.flip(imagem_renderizada, 0)
    imagem_renderizada = cv2.cvtColor(imagem_renderizada, cv2.COLOR_RGB2BGR)  # Converter RGB → BGR

    # Criar uma máscara onde os pixels pretos indicam transparência
    gray = cv2.cvtColor(imagem_renderizada, cv2.COLOR_BGR2GRAY)  # Converter para tons de cinza
    _, mask = cv2.threshold(gray, 1, 255, cv2.THRESH_BINARY)  # Criar máscara: 0 para preto, 255 para o resto

    # Inverter a máscara para pegar apenas o fundo
    mask_inv = cv2.bitwise_not(mask)

    # Criar uma versão da imagem de fundo com buraco onde os objetos estão
    fundo_com_buraco = cv2.bitwise_and(imagem_fundo, imagem_fundo, mask=mask_inv)

    # Criar uma versão da renderização que mantém apenas os objetos
    objetos_renderizados = cv2.bitwise_and(imagem_renderizada, imagem_renderizada, mask=mask)

    # Combinar as duas imagens corretamente
    resultado = cv2.add(fundo_com_buraco, objetos_renderizados)
    return resultado

def get_homography(frame_base, frame_obj, detector, matcher):
    kp_base, des_base = detector.detectAndCompute(frame_base, None)
    kp_obj, des_obj = detector.detectAndCompute(frame_obj, None)
    
    matches = matcher.match(des_base, des_obj)
    matches = sorted(matches, key=lambda x: x.distance)
    num_matches = 50
    matches = matches[:num_matches]

    src_pts = np.float32([kp_base[m.queryIdx].pt for m in matches]).reshape(-1,1,2)
    dst_pts = np.float32([kp_obj[m.trainIdx].pt for m in matches]).reshape(-1,1,2)

    H, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)

    return H

def distance_is_minimal(east_0, north_0, h_0, east_1, north_1, h_1):
    point_0 = np.array([east_0, north_0, h_0])
    point_1 = np.array([east_1, north_1, h_1])
    distance = np.linalg.norm(point_1 - point_0)
    if distance < h_1 * minimal_distance_param:
        return True
    else:
        return False

def get_DEM_alt(east_utm, north_utm):
    row, col = ~dem_transform * (east_utm, north_utm)
    row = int(round(row))
    col = int(round(col))
    if 0 <= row < dem_elevation_data.shape[0] and 0 <= col < dem_elevation_data.shape[1]:
        return dem_elevation_data[row, col]
    else:
        return None  # Fora da imagem

def get_R_one_roi(roi_enu, roi_pixel, R, K_inv, t_drone_ENU):    
    theta_1, R_1 = get_rotation_from_vectors(R @ (roi_enu - t_drone_ENU), K_inv @ roi_pixel)
    theta_2, R_2 = get_rotation_from_vectors(R @ (roi_enu - t_drone_ENU), - K_inv @ roi_pixel)
    if theta_1 <= theta_2:
        R_corr = R_1
    else:
        R_corr = R_2
    
    return R_corr @ R

def get_R_roi(roi_enus, roi_pixels, K_inv, t_drone_ENU):
    if len(roi_enus) > 2:
        print("CORRECAO PARA 3 OU MAIS ROI AINDA A IMPLEMENTAR")
        print("CONSIDERAMOS SOMENTE OS DOIS PRIMEIROS ROI")
    
    a_list = [(lambda a: norm_vec(a - t_drone_ENU))(a) for a in roi_enus]
    b_list = [(lambda b: norm_vec(K_inv @ b))(b) for b in roi_pixels]

    u_0 = a_list[0]
    u_1 = norm_vec(a_list[1] - (np.dot(a_list[1].copy().flatten(), u_0.copy().flatten())) * u_0)
    u_2_flat = np.cross(u_0.copy().flatten(), u_1.copy().flatten())
    u_2 = np.array([[u_2_flat[0]],[u_2_flat[1]],[u_2_flat[2]]])
    
    v_0 = b_list[0]
    v_1 = norm_vec(b_list[1] - (np.dot(b_list[1].copy().flatten(), v_0.copy().flatten())) * v_0)
    v_2_flat = np.cross(v_0.copy().flatten(), v_1.copy().flatten())
    v_2 = np.array([[v_2_flat[0]],[v_2_flat[1]],[v_2_flat[2]]])

    A = np.concatenate((u_0, u_1, u_2), axis=1)
    B = np.concatenate((v_0, v_1, v_2), axis=1)

    return B @ np.transpose(A)

def instantiate(K, R, t, point, color, t_drone_ENU, pitch):
    pixel = K @ np.concatenate((R, t), axis=1) @ np.vstack((point, [1]))
    pixel = pixel.flatten()
    pixel = pixel / pixel[2]
    if color == "red":
        colorN = (0,0,255)
    elif color == "black":
        colorN = (0,0,0)
    elif color == "blue":
        colorN = (255,0,0)
    elif color == "green":
        colorN = (0,255,0)

    desenhar_centro(image, int(pixel[0]), int(pixel[1]), colorN)
    print_on_pixel(image, f"N:{point[1,0]:.3f}, E:{point[0,0]:.3f}, Up: {point[2,0]:.3f}", int(pixel[0]), int(pixel[1]), colorN)
    t_opengl = cameraToOpenglR @ R @ (point - t_drone_ENU + [[0],[0],[cone_height]])
    view_matrix = build_view_matrix(cameraToOpenglR @ R, np.array([[0],[0],[0]]))
    glMatrixMode(GL_MODELVIEW)
    glLoadMatrixf(view_matrix)
    render(lambda: draw_cone_sphere(t_opengl[0,0], t_opengl[1,0], t_opengl[2,0], pitch, color))


with open("parameters.json", "r") as json_file:
    parameters = json.load(json_file)

K_path = parameters["K_path"]
with open(K_path, "r") as json_file:
    K = np.array(json.load(json_file), dtype=np.float64)

dem_elevation_data = None
try:
    tif_path = parameters["tif_path"]
    with rasterio.open(tif_path) as dem_dataset:
        dem_elevation_data = dem_dataset.read(1)
        dem_transform = dem_dataset.transform
        dem_crs = dem_dataset.crs
except Exception as e:
    print(f"Error: {e}\nConsidering flat terrain...")

if dem_elevation_data is not None:
    h0_dem = get_DEM_alt(utm0_x, utm0_y)
    if h0_dem is not None:
        h_dem_offset = h0 - h0_dem
    else:
        raise Exception("Origem do sistema de coordenadas fora do mapa de elevação carregado!")

# Inicializar GLFW
if not glfw.init():
    raise Exception("GLFW não pôde ser inicializado!")

cuda_count = cv2.cuda.getCudaEnabledDeviceCount()
gsrc = cv2.cuda.GpuMat()
gtemplate = cv2.cuda.GpuMat()
gresult = cv2.cuda.GpuMat()
if cuda_count != 0:
    print("CUDA enabled")
    cuda_matcher = cv2.cuda.createTemplateMatching(cv2.CV_8UC1, cv2.TM_CCOEFF_NORMED)

# Criar janela OpenGL
window = glfw.create_window(1920, 1080, "Render 3D", None, None)
glfw.make_context_current(window)

glEnable(GL_DEPTH_TEST)

# Ativar iluminação
glEnable(GL_LIGHTING)

# Criar e ativar uma luz
glEnable(GL_LIGHT0)

# Definir a posição da luz (x, y, z, w)
light_position = [0, 3, 3, 1]  # (x=0, y=3, z=3, w=1 para luz pontual)
glLightfv(GL_LIGHT0, GL_POSITION, light_position)

# Definir intensidade da luz ambiente, difusa e especular
light_ambient = [0.2, 0.2, 0.2, 1.0]  # Luz fraca no ambiente
light_diffuse = [0.8, 0.8, 0.8, 1.0]  # Luz principal
light_specular = [1.0, 1.0, 1.0, 1.0]  # Reflexo especular forte

glLightfv(GL_LIGHT0, GL_AMBIENT, light_ambient)
glLightfv(GL_LIGHT0, GL_DIFFUSE, light_diffuse)
glLightfv(GL_LIGHT0, GL_SPECULAR, light_specular)

# Ativar normalização de vetores normais (evita distorções)
glEnable(GL_NORMALIZE)

# Configurar matriz de projeção
proj_matrix = build_projection_matrix(K, 1920, 1080)
glMatrixMode(GL_PROJECTION)
glLoadMatrixf(np.transpose(proj_matrix))

K_inv = inv_K(K)

project_id = "car-models-rr7w5"
model_version = 1
api_key = parameters["api_key"]
api_url = parameters["api_url"]

client = InferenceHTTPClient(api_url=api_url, api_key=api_key)

source = parameters["video_path"]
cap = cv2.VideoCapture(source)

frame_info = parse_srt(parameters["video_data_path"])
frame_index = 0

original_width = 1920
original_height = 1080
resized_width = parameters["resized_width"]
resized_height = parameters["resized_height"]
scale_x = original_width / resized_width
scale_y = original_height / resized_height
window_name = "Locate"

# # Homography stuff
# frame_gap = 10
# orb = cv2.ORB_create(nfeatures=1000)
# bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

get_roi = False
image_roi_gray_list = []
roi_data_list = []
roi_pixel_list = []
roi_confidence_list = []
good_roi_list = []
good_roi_data_list = []

scale_reduct_inference = 6

clicks = deque(maxlen=10)
clicks_ENU = deque(maxlen=10)

# Localizacao carro: [latitude: -22.905551] [longitude: -43.221218] [rel_alt: 2.847 abs_alt: 15.331] 15.331 - 2.847 = 12.484
car_x, car_y, car_z = enu.geodetic2enu(-22.905551, -43.221218, 12.484, lat0, lon0, h0)
t_car_mundo = np.array([[car_x],[car_y],[car_z]])

play = True
images = []
while not glfw.window_should_close(window):
    
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

    ret, image = cap.read()
    if ret:
        images.append(image)
        if play:
            frame_index += 1

    key = cv2.waitKey(1)
    if key & 0xFF == ord('q'):
        break
    elif key & 0xFF == ord('d'):
        if frame_index + 1 < len(images):
            frame_index += 1
        continue
    elif key & 0xFF == ord('f'):
        if frame_index + 10 < len(images):
            frame_index += 10
        continue
    elif key & 0xFF == ord('a'):
        frame_index -= 10
        if frame_index < 1:
            frame_index = 1
        continue
    elif key & 0xFF == ord('g'):
        glMode = not glMode
        continue
    elif key & 0xFF == ord('s'):
        get_roi = True
        continue
    elif key & 0xFF == ord(' '):
        play = not play
    
    image = images[frame_index - 1 if frame_index > 0 else 0].copy()
    image_gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    roi_pixel_list.clear()
    roi_confidence_list.clear()
    good_roi_list.clear()
    good_roi_data_list.clear()
    R_roi = None

    yaw = float(frame_info[frame_index]['gb_yaw'])
    pitch = float(frame_info[frame_index]['gb_pitch'])
    roll = float(frame_info[frame_index]['gb_roll'])
    R_drone = yaw_pitch_roll_to_rotation_matrix(yaw, pitch, roll)
    R_drone_T = np.transpose(R_drone)

    h_rel = float(frame_info[frame_index]['rel_alt'])
    h_abs = float(frame_info[frame_index]['abs_alt'])
    lat = float(frame_info[frame_index]['latitude'])
    long = float(frame_info[frame_index]['longitude'])

    easting, northing, h_enu = enu.geodetic2enu(lat, long, h_abs, lat0, lon0, h0)

    # # Homography stuff
    # R_alt = None
    # homography_index = frame_index - frame_gap if frame_index > frame_gap + 1 else None
    # if homography_index is not None:
    #     image_base = images[homography_index - 1].copy()
    #     lat_base = float(frame_info[homography_index]['latitude'])
    #     long_base = float(frame_info[homography_index]['longitude'])
    #     h_abs_base = float(frame_info[homography_index]['abs_alt'])
    #     yaw_base = float(frame_info[homography_index]['gb_yaw'])
    #     pitch_base = float(frame_info[homography_index]['gb_pitch'])
    #     roll_base = float(frame_info[homography_index]['gb_roll'])

    #     R_drone_base = yaw_pitch_roll_to_rotation_matrix(yaw_base, pitch_base, roll_base)
    #     R_drone_base_T = np.transpose(R_drone_base)
    #     easting_base, northing_base, h_enu_base = enu.geodetic2enu(lat_base, long_base, h_abs_base, lat0, lon0, h0)
        
    #     if distance_is_minimal(easting_base, northing_base, h_enu_base, easting, northing, h_enu):
    #         image_base_gray = cv2.cvtColor(image_base, cv2.COLOR_BGR2GRAY)
    #         H = get_homography(image_base_gray, image_gray, orb, bf)
    #         R_hom = K_inv @ H @ K
    #         R_alt = R_hom @ droneToCameraR @ R_drone_base_T @ mundoToDroneR

    t_drone_mundo = np.array([[easting], [northing], [h_enu]])
    print_on_pixel(image, f"index:{frame_index}, N:{int(northing)}, E:{int(easting)}, h_rel:{h_rel}, yaw:{yaw}, pitch:{pitch}, roll:{roll}", 10, 10, (0,0,0))

    R = droneToCameraR @ R_drone_T @ mundoToDroneR

    if get_roi:
        rois = cv2.selectROIs("Select ROIs", image)
        cv2.destroyWindow("Select ROIs")
        for i,roi in enumerate(rois):
            x, y, w, h = roi
            image_roi = image[y:y+h, x:x+w]
            image_roi_gray_list.append(cv2.cvtColor(image_roi, cv2.COLOR_BGR2GRAY))
            roi_data = get_roi_data(i)
            roi_data_list.append(roi_data)
        get_roi = False
    
    for i,image_roi_gray in enumerate(image_roi_gray_list):
        if cuda_count == 0:
            templ_match = cv2.matchTemplate(image_gray, image_roi_gray, cv2.TM_CCOEFF_NORMED)
        else:
            gsrc.upload(image_gray)
            gtemplate.upload(image_roi_gray)
            gresult = cuda_matcher.match(gsrc, gtemplate)
            templ_match = gresult.download()
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(templ_match)
        w, h = image_roi_gray.shape[::-1]
        roi_x = max_loc[0] + w/2
        roi_y = max_loc[1] + h/2
        roi_pixel = np.array([[roi_x], [roi_y], [1]])
        roi_pixel_list.append(roi_pixel)
        roi_confidence_list.append(max_val)
        desenhar_centro(image, int(roi_x), int(roi_y), (100, 0, 100), roi_flag=True)
        print_on_pixel(image, f"ROI similarity: {max_val:.3f}", int(roi_x), int(roi_y), (100, 0, 100))
    
    for i,roi_confidence in enumerate(roi_confidence_list):
        if roi_confidence > roi_minimum_confidence:
            good_roi_list.append(roi_pixel_list[i])
            lat_roi, long_roi, h_abs_roi = roi_data_list[i]
            easting_roi, northing_roi, h_enu_roi = enu.geodetic2enu(lat_roi, long_roi, h_abs_roi, lat0, lon0, h0)
            roi_enu = np.array([[easting_roi],[northing_roi],[h_enu_roi]])
            good_roi_data_list.append(roi_enu)
    
    if len(good_roi_list) == 1:
        R_roi = get_R_one_roi(good_roi_data_list[0], good_roi_list[0], R, K_inv, t_drone_mundo)
    elif len(good_roi_list) >= 2:
        R_roi = get_R_roi(good_roi_data_list, good_roi_list, K_inv, t_drone_mundo)
    
    t =  - R @ t_drone_mundo

    # Carro
    pixel_car = K @ np.concatenate((R, t), axis=1) @ np.vstack((t_car_mundo, [1]))
    pixel_car = pixel_car.flatten()
    pixel_car = pixel_car / pixel_car[2]
    instantiate(K, R, t, t_car_mundo, "red", t_drone_mundo, pitch)

    # # Homography stuff
    # if R_alt is not None:
    #     pixel_car_alt = K @ np.concatenate((R_alt, - R_alt @ t_drone_mundo), axis=1) @ np.vstack((t_car_mundo, [1]))
    #     pixel_car_alt = pixel_car_alt.flatten()
    #     pixel_car_alt = pixel_car_alt / pixel_car_alt[2]
    #     desenhar_centro(image, int(pixel_car_alt[0] / scale_x), int(pixel_car_alt[1] / scale_y), (255,0,0))
    #     print_on_pixel(image, f"N:{t_car_mundo[1,0]}, E:{t_car_mundo[0,0]}", int(pixel_car_alt[0] / scale_x), int(pixel_car_alt[1] / scale_y), (255,0,0))
    #     t_car_opengl_alt = cameraToOpenglR @ R_alt @ (t_car_mundo - t_drone_mundo + [[0],[0],[cone_height]])
    #     render(lambda: draw_cone_sphere(t_car_opengl_alt[0,0], t_car_opengl_alt[1,0], t_car_opengl_alt[2,0], pitch, "blue"))


    # Origem coordenada ENU
    instantiate(K, R, t, np.array([[0],[0],[0]]), "black", t_drone_mundo, pitch)

    for click in clicks:
        reta = reta3D(K_inv, droneToMundoR @ R_drone @ cameraToDroneR, t_drone_mundo, (click[0], click[1]))
        # click_ENU = find_ground_intersection_ENU(northing, easting, h_enu, reta[1])
        vec_DEM = norm_vec(reta[1].flatten())
        if vec_DEM[2] < 0:
            vec_DEM = (-1) * vec_DEM
        if dem_elevation_data is not None:
            click_ENU = find_DEM_intersection(easting + utm0_x, northing + utm0_y, h_abs - h_dem_offset, vec_DEM)
        else:
            click_ENU = find_ground_intersection_ENU(northing, easting, h_enu, vec_DEM)
        if click_ENU is not None:
            if dem_elevation_data is not None:
                click_ENU[0,0] -= utm0_x
                click_ENU[1,0] -= utm0_y
                click_ENU[2,0] += h_dem_offset - h0
            erro_car = np.linalg.norm(click_ENU - t_car_mundo)
            dist_drone = np.linalg.norm(t_drone_mundo - t_car_mundo)
            # Frame; Erro; Altura do Drone; Distância do Drone; Click ENU; Click Pixel; Car Pixel; Drone ENU
            print(f"{frame_index}; {erro_car}; {h_rel}; {dist_drone}; {click_ENU.copy().flatten()}; {(click[0], click[1])}; {(pixel_car[0], pixel_car[1])}; {t_drone_mundo.copy().flatten()}")
            clicks_ENU.append(click_ENU)

    clicks.clear()
    clicks_ENU_copy = clicks_ENU.copy()

    for enu_click in clicks_ENU_copy:
        instantiate(K, R, t, enu_click, "blue", t_drone_mundo, pitch)
        if R_roi is not None:
            instantiate(K, R_roi, - R_roi @ t_drone_mundo, enu_click, "green", t_drone_mundo, pitch)
    
    glfw.poll_events()
    glfw.swap_buffers(window)
    
    pixels = glReadPixels(0, 0, 1920, 1080, GL_RGB, GL_UNSIGNED_BYTE)
    image = draw_opengl(pixels, image)
    
    # # IA detection stuff
    # short_image = cv2.resize(image, (int(original_width / scale_reduct_inference), int(original_height / scale_reduct_inference)))
    # results = client.infer(short_image, model_id=f"{project_id}/{model_version}")

    # for prediction in results['predictions']:
                        
    #     width, height = int(prediction['width'] * scale_reduct_inference), int(prediction['height'] * scale_reduct_inference)
    #     prediction_x = int(prediction['x'] * scale_reduct_inference)
    #     prediction_y = int(prediction['y'] * scale_reduct_inference)

    #     x, y = int(prediction_x - width/2) , int(prediction_y - height/2)
        
    #     class_id = prediction['class_id']

    #     # Calculate the bottom right x and y coordinates
    #     x2 = int(x + width)
    #     y2 = int(y + height)

    #     if class_id == 0:
    #         cv2.rectangle(image, (x, y), (x2, y2), (0, 0, 255), 3)
    #         desenhar_centro(image, int(prediction_x), int(prediction_y), (0, 0, 255))

    #         reta = reta3D(K_inv, droneToMundoR @ R_drone @ cameraToDroneR, t_drone_mundo, (prediction_x, prediction_y))
    #         pred_UTM = find_ground_intersection_UTM(northing, easting, h, h_abs, reta[1])
    #         print_on_pixel(image, f"N:{pred_UTM[1]}, E:{pred_UTM[0]}, ZN:{zone_number}, ZL:{zone_letter}", x, y, (0, 0, 255))
    
    rez_img = cv2.resize(image, (resized_width, resized_height))
    cv2.imshow(window_name, rez_img)
    cv2.setMouseCallback(window_name, mouse_click, (clicks, clicks_ENU))